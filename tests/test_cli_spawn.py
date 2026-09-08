"""Unit tests for CLI pane spawning command construction."""

from __future__ import annotations

import base64
import re
import subprocess
import types

from teammate_mcp import cli
from teammate_mcp import daemon_client
from teammate_mcp import registry


def _decode_spawn_payload(applescript: str) -> str:
    match = re.search(r"echo ([A-Za-z0-9+/=]+) \| base64 -d", applescript)
    assert match, applescript
    return base64.b64decode(match.group(1)).decode("utf-8")


def test_spawn_codex_builds_codex_command_not_claude(tmp_path, monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, stdout="SID-CODEX\n", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli, "_append_spawn_ledger", lambda record: None)

    rc = cli._cmd_spawn([
        "codex-worker",
        "codex",
        "--yolo",
        "--cwd",
        str(tmp_path),
        "--no-wait",
    ])

    assert rc == 0
    payload = _decode_spawn_payload(captured["cmd"][2])
    assert 'export TEAMMATE_LABEL="codex-worker"' in payload
    assert f'cd "{tmp_path}"' in payload
    assert payload.endswith('/bin/tmcodex"')
    assert "tmcodex" in payload
    assert "claude" not in payload


def test_spawn_claude_builds_claude_command_not_codex(tmp_path, monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="SID-CLAUDE\n", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli, "_append_spawn_ledger", lambda record: None)

    rc = cli._cmd_spawn([
        "claude-worker",
        "claude",
        "--dangerously-skip-permissions",
        "--cwd",
        str(tmp_path),
        "--no-wait",
    ])

    assert rc == 0
    payload = _decode_spawn_payload(captured["cmd"][2])
    assert 'export TEAMMATE_LABEL="claude-worker"' in payload
    assert payload.endswith('/bin/tmclaude"')
    assert "tmclaude" in payload
    assert "codex" not in payload


def test_split_spawn_applescript_targets_anchor_session():
    script = cli._build_spawn_applescript(
        "split-v",
        "echo hi",
        anchor_session_id="ANCHOR-SID",
    )

    assert 'if ((unique id of s) as string) is "ANCHOR-SID"' in script
    assert "set anchorWindow to w" in script
    assert "split vertically with default profile" in script
    assert "current session of current window" not in script


def test_tab_spawn_applescript_targets_anchor_window():
    script = cli._build_spawn_applescript(
        "tab",
        "echo hi",
        anchor_session_id="ANCHOR-SID",
    )

    assert 'if ((unique id of s) as string) is "ANCHOR-SID"' in script
    assert "tell anchorWindow" in script
    assert "create tab with default profile" in script
    assert "current window" not in script


def test_spawn_split_defaults_anchor_to_calling_term_session(tmp_path, monkeypatch):
    captured = {}

    def fake_build(mode, shell_line, bounds=None, anchor_session_id=""):
        captured["mode"] = mode
        captured["anchor_session_id"] = anchor_session_id
        return "return \"SID-NEW\""

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="SID-NEW\n", stderr="")

    monkeypatch.delenv("TEAMMATE_LABEL", raising=False)
    monkeypatch.setenv("TERM_SESSION_ID", "w1t0p1:CALLER-SID")
    monkeypatch.setattr(cli, "_build_spawn_applescript", fake_build)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli, "_append_spawn_ledger", lambda record: None)

    rc = cli._cmd_spawn([
        "anchored-worker",
        "zsh",
        "--mode",
        "split-v",
        "--cwd",
        str(tmp_path),
        "--no-wait",
    ])

    assert rc == 0
    assert captured["mode"] == "split-v"
    assert captured["anchor_session_id"] == "CALLER-SID"


def test_spawn_default_mode_is_split_of_caller_pane(tmp_path, monkeypatch):
    """No --mode given: the default layout splits the caller's pane
    (not a new window/tab, not the focused pane)."""
    captured = {}

    def fake_build(mode, shell_line, bounds=None, anchor_session_id=""):
        captured["mode"] = mode
        captured["anchor_session_id"] = anchor_session_id
        return "return \"SID-NEW\""

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="SID-NEW\n", stderr="")

    monkeypatch.delenv("TEAMMATE_LABEL", raising=False)
    monkeypatch.setenv("TERM_SESSION_ID", "w1t0p1:CALLER-SID")
    monkeypatch.setattr(cli, "_load_spawn_ledger", lambda: {})  # no prior children
    monkeypatch.setattr(cli, "_build_spawn_applescript", fake_build)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli, "_append_spawn_ledger", lambda record: None)

    rc = cli._cmd_spawn(["worker", "zsh", "--cwd", str(tmp_path), "--no-wait"])

    assert rc == 0
    assert captured["mode"] == "split-v"
    assert captured["anchor_session_id"] == "CALLER-SID"


def test_spawn_default_anchor_prefers_teammate_label_over_term_session(tmp_path, monkeypatch):
    """Caller pane is resolved via TEAMMATE_LABEL→registry FIRST, so a
    misleading TERM_SESSION_ID (e.g. Claude Code's bash subprocess carries
    a different session id than the pane that launched the MCP server)
    does not cause the wrong pane to be split."""
    captured = {}

    def fake_build(mode, shell_line, bounds=None, anchor_session_id=""):
        captured["mode"] = mode
        captured["anchor_session_id"] = anchor_session_id
        return "return \"SID-NEW\""

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="SID-NEW\n", stderr="")

    monkeypatch.setenv("TEAMMATE_LABEL", "claude3")
    monkeypatch.setenv("TERM_SESSION_ID", "w9t9p9:WRONG-BASH-SID")
    monkeypatch.setattr(cli, "_load_spawn_ledger", lambda: {})  # no prior children
    monkeypatch.setattr(
        registry, "lookup",
        lambda label: {"session_id": "w0t0p0:REAL-CLAUDE3-SID"} if label == "claude3" else None,
    )
    monkeypatch.setattr(cli, "_build_spawn_applescript", fake_build)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli, "_append_spawn_ledger", lambda record: None)

    rc = cli._cmd_spawn(["worker", "zsh", "--cwd", str(tmp_path), "--no-wait"])

    assert rc == 0
    assert captured["mode"] == "split-v"
    assert captured["anchor_session_id"] == "REAL-CLAUDE3-SID"


def test_spawn_falls_back_to_window_when_caller_pane_unresolvable(tmp_path, monkeypatch):
    """If neither TEAMMATE_LABEL nor TERM_SESSION_ID identifies the caller
    pane, open a new window — never split iTerm's focused pane."""
    captured = {}

    def fake_build(mode, shell_line, bounds=None, anchor_session_id=""):
        captured["mode"] = mode
        captured["anchor_session_id"] = anchor_session_id
        return "return \"SID-NEW\""

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="SID-NEW\n", stderr="")

    monkeypatch.delenv("TEAMMATE_LABEL", raising=False)
    monkeypatch.delenv("TERM_SESSION_ID", raising=False)
    monkeypatch.setattr(cli, "_build_spawn_applescript", fake_build)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli, "_append_spawn_ledger", lambda record: None)

    rc = cli._cmd_spawn(["worker", "zsh", "--cwd", str(tmp_path), "--no-wait"])

    assert rc == 0
    assert captured["mode"] == "window"
    assert captured["anchor_session_id"] == ""


def test_spawn_screen_region_forces_window_over_default_split(tmp_path, monkeypatch):
    """--screen implies a positioned window; it overrides the default split
    layout when --mode is not explicitly given."""
    captured = {}

    def fake_build(mode, shell_line, bounds=None, anchor_session_id=""):
        captured["mode"] = mode
        captured["bounds"] = bounds
        return "return \"SID-NEW\""

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="SID-NEW\n", stderr="")

    monkeypatch.setenv("TEAMMATE_LABEL", "claude3")
    monkeypatch.setattr(registry, "lookup", lambda label: {"session_id": "REAL"})
    monkeypatch.setattr(cli, "_resolve_screen_region", lambda region: (0, 0, 100, 100))
    monkeypatch.setattr(cli, "_build_spawn_applescript", fake_build)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli, "_append_spawn_ledger", lambda record: None)

    rc = cli._cmd_spawn([
        "worker", "zsh", "--screen", "right", "--cwd", str(tmp_path), "--no-wait",
    ])

    assert rc == 0
    assert captured["mode"] == "window"
    assert captured["bounds"] == (0, 0, 100, 100)


# --- SORANO-style stacking placement (the spawn default) ----------------

def test_auto_placement_first_child_splits_caller(monkeypatch):
    monkeypatch.setattr(cli, "_load_spawn_ledger", lambda: {})
    monkeypatch.setattr(registry, "all_labels", lambda: {})
    assert cli._auto_placement("CALLER") == ("split-v", "CALLER")


def test_auto_placement_no_caller_returns_window():
    assert cli._auto_placement("") == ("window", "")


def test_auto_placement_subsequent_child_stacks_under_last_live(monkeypatch):
    ledger = {
        "w1": {"session_id": "SID-W1", "spawner_session_id": "CALLER", "spawned_at": 1},
        "w2": {"session_id": "SID-W2", "spawner_session_id": "CALLER", "spawned_at": 2},
        "x":  {"session_id": "SID-X",  "spawner_session_id": "OTHER",  "spawned_at": 3},
    }
    monkeypatch.setattr(cli, "_load_spawn_ledger", lambda: ledger)
    monkeypatch.setattr(registry, "all_labels", lambda: {
        "w1": {"session_id": "SID-W1"},
        "w2": {"session_id": "SID-W2"},
        "x":  {"session_id": "SID-X"},
    })
    # split-h, anchored under the LAST (most recent) live child of CALLER —
    # the child of OTHER is ignored.
    assert cli._auto_placement("CALLER") == ("split-h", "SID-W2")


def test_auto_placement_ignores_dead_children(monkeypatch):
    ledger = {"w1": {"session_id": "SID-W1", "spawner_session_id": "CALLER", "spawned_at": 1}}
    monkeypatch.setattr(cli, "_load_spawn_ledger", lambda: ledger)
    monkeypatch.setattr(registry, "all_labels", lambda: {})  # w1 no longer alive
    # No LIVE children → back to first-child split-v off the caller.
    assert cli._auto_placement("CALLER") == ("split-v", "CALLER")


def test_spawn_default_stacks_under_last_child(tmp_path, monkeypatch):
    """No --mode/--anchor + an existing live child → split-h under it."""
    captured = {}

    def fake_build(mode, shell_line, bounds=None, anchor_session_id=""):
        captured["mode"] = mode
        captured["anchor_session_id"] = anchor_session_id
        return "return \"SID-NEW\""

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="SID-NEW\n", stderr="")

    monkeypatch.delenv("TEAMMATE_LABEL", raising=False)
    monkeypatch.setenv("TERM_SESSION_ID", "w1t0p1:CALLER")
    monkeypatch.setattr(cli, "_load_spawn_ledger", lambda: {
        "w1": {"session_id": "SID-W1", "spawner_session_id": "CALLER", "spawned_at": 1},
    })
    monkeypatch.setattr(registry, "all_labels", lambda: {"w1": {"session_id": "SID-W1"}})
    monkeypatch.setattr(cli, "_build_spawn_applescript", fake_build)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli, "_append_spawn_ledger", lambda record: None)

    rc = cli._cmd_spawn(["w2", "zsh", "--cwd", str(tmp_path), "--no-wait"])

    assert rc == 0
    assert captured["mode"] == "split-h"            # stacked, not split-v
    assert captured["anchor_session_id"] == "SID-W1"  # under the last live child


def test_spawn_split_anchor_option_resolves_registered_label(tmp_path, monkeypatch):
    captured = {}

    def fake_build(mode, shell_line, bounds=None, anchor_session_id=""):
        captured["anchor_session_id"] = anchor_session_id
        return "return \"SID-NEW\""

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="SID-NEW\n", stderr="")

    monkeypatch.setattr(cli, "_build_spawn_applescript", fake_build)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli, "_append_spawn_ledger", lambda record: None)
    monkeypatch.setattr(registry, "lookup", lambda label: {"session_id": "ANCHOR-REGISTERED"})

    rc = cli._cmd_spawn([
        "anchored-worker",
        "zsh",
        "--mode",
        "split-h",
        "--anchor",
        "codex1",
        "--cwd",
        str(tmp_path),
        "--no-wait",
    ])

    assert rc == 0
    assert captured["anchor_session_id"] == "ANCHOR-REGISTERED"


def test_spawn_split_anchor_option_accepts_term_session_id(tmp_path, monkeypatch):
    captured = {}

    def fake_build(mode, shell_line, bounds=None, anchor_session_id=""):
        captured["anchor_session_id"] = anchor_session_id
        return "return \"SID-NEW\""

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="SID-NEW\n", stderr="")

    monkeypatch.setattr(cli, "_build_spawn_applescript", fake_build)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli, "_append_spawn_ledger", lambda record: None)
    monkeypatch.setattr(registry, "lookup", lambda label: None)

    rc = cli._cmd_spawn([
        "anchored-worker",
        "zsh",
        "--mode",
        "split-v",
        "--anchor",
        "w1t0p1:RAW-SESSION-ID",
        "--cwd",
        str(tmp_path),
        "--no-wait",
    ])

    assert rc == 0
    assert captured["anchor_session_id"] == "RAW-SESSION-ID"


def test_despawn_skips_label_reused_with_different_session(monkeypatch, capsys):
    ledger = {
        "worker": {
            "label": "worker",
            "session_id": "OLD-SID",
            "command": "claude",
        },
    }
    monkeypatch.setattr(cli, "_load_spawn_ledger", lambda: dict(ledger))
    monkeypatch.setattr(cli, "_save_spawn_ledger", lambda records: ledger.clear() or ledger.update(records))
    monkeypatch.setattr(cli, "_close_iterm_session", lambda sid: (_ for _ in ()).throw(
        AssertionError("must not close a stale ledger sid for a reused label")
    ))
    monkeypatch.setattr(registry, "all_labels", lambda: {"worker": {"session_id": "NEW-SID"}})
    monkeypatch.setattr(registry, "unregister", lambda label: (_ for _ in ()).throw(
        AssertionError("must not unregister a reused label")
    ))
    monkeypatch.setattr(daemon_client, "is_enabled", lambda: False)

    rc = cli._cmd_despawn(["worker"])

    assert rc == 1
    assert ledger["worker"]["session_id"] == "OLD-SID"
    assert "label reused" in capsys.readouterr().err


def test_despawn_does_not_unregister_reused_label_when_old_sid_is_gone(monkeypatch):
    saved = {}
    monkeypatch.setattr(cli, "_load_spawn_ledger", lambda: {
        "worker": {
            "label": "worker",
            "session_id": "OLD-SID",
            "command": "claude",
        },
    })
    monkeypatch.setattr(cli, "_save_spawn_ledger", lambda records: saved.update(records))
    monkeypatch.setattr(cli, "_close_iterm_session", lambda sid: False)
    monkeypatch.setattr(registry, "all_labels", lambda: {"worker": {"session_id": "NEW-SID"}})
    monkeypatch.setattr(registry, "unregister", lambda label: (_ for _ in ()).throw(
        AssertionError("must not unregister a reused label")
    ))
    monkeypatch.setattr(daemon_client, "is_enabled", lambda: False)

    rc = cli._cmd_despawn(["--gone"])

    assert rc == 0
    assert saved == {}


def test_register_pane_uses_live_sid_fallback_when_term_session_id_missing(monkeypatch):
    monkeypatch.delenv("TERM_SESSION_ID", raising=False)
    monkeypatch.delenv("ITERM_SESSION_ID", raising=False)

    seen = []

    def fake_resolve(sid_tail):
        seen.append(sid_tail)
        return "LIVE-SID"

    monkeypatch.setattr(cli, "_resolve_live_sid_via_ppid", fake_resolve)
    monkeypatch.setattr(cli.asyncio, "run", lambda coro: coro.close() or 0)

    rc = cli._cmd_register_pane([])

    assert rc == 0
    assert seen == ["", "LIVE-SID"]


def test_whoami_uses_live_sid_fallback_when_term_session_id_missing(monkeypatch, capsys):
    monkeypatch.delenv("TERM_SESSION_ID", raising=False)
    monkeypatch.delenv("ITERM_SESSION_ID", raising=False)

    monkeypatch.setattr(cli, "_resolve_live_sid_via_ppid", lambda sid_tail: "LIVE-SID")
    monkeypatch.setattr(
        registry,
        "all_labels",
        lambda: {"worker1": {"session_id": "LIVE-SID"}},
    )

    rc = cli._cmd_whoami()

    assert rc == 0
    assert capsys.readouterr().out.strip() == "worker1"
