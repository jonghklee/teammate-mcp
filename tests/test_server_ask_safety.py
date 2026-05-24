"""Unit tests for ask delivery safety helpers."""

from __future__ import annotations

import json
import sys
import types

import pytest

from teammate_mcp import server


def test_sanitize_for_inject_removes_tui_control_bytes():
    raw = "hello\x1b[200~\x07\x00\nworld\tok"

    out = server._sanitize_for_inject(raw)

    assert "\x1b" not in out
    assert "\x07" not in out
    assert "\x00" not in out
    assert "\nworld\tok" in out


# NOTE: picker-detection tests removed — _pane_looks_like_picker was
# deleted in 888f98e ("remove picker/danger detection — assume
# picker-free environment"). The environment now blocks picker tools
# via permissions.deny, so the heuristic no longer exists.


def test_body_stuck_detection_ignores_old_busy_markers_in_scrollback():
    pane = "\n".join([
        "✻ Brewed for 2m 42s",
        "old assistant output",
        "────────────────────",
        "❯ [teammate-mcp ASK job-123 from=codex1]",
        "  body still in compose",
        "────────────────────",
        "bypass permissions on · shift+tab to cycle",
    ])

    assert server._body_stuck_in_compose(pane, "job-123")


def test_body_stuck_detection_treats_current_busy_tail_as_not_stuck():
    pane = "\n".join([
        "❯ [teammate-mcp ASK job-123 from=codex1]",
        "  body submitted",
        "✻ Nucleating…",
    ])

    assert not server._body_stuck_in_compose(pane, "job-123")


@pytest.mark.asyncio
async def test_ask_mailbox_only_writes_inbox_without_injecting(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "MAILBOX_ROOT", tmp_path / "mailbox")
    monkeypatch.setenv("TEAMMATE_LABEL", "sender")
    monkeypatch.setattr(server, "_resolve_target_session_id", lambda target, fallback: "SID-TARGET")
    monkeypatch.setattr(server, "osa_session_alive", lambda sid: True)

    def fail_inject(*args, **kwargs):
        raise AssertionError("mailbox-only ask must not inject keystrokes")

    monkeypatch.setattr(server, "osa_clear_and_inject", fail_inject)
    monkeypatch.setattr(server, "osa_send_raw", fail_inject)

    answer = await server._ask_async("hello", target="receiver", mailbox_only=True)

    assert answer == "queued mailbox-only message for receiver"
    inbox_files = list((tmp_path / "mailbox" / "receiver" / "inbox").glob("*.json"))
    assert len(inbox_files) == 1
    record = json.loads(inbox_files[0].read_text(encoding="utf-8"))
    assert record["from_"] == "sender"
    assert record["to"] == "receiver"
    assert record["body"] == "hello"


@pytest.mark.asyncio
async def test_ask_defaults_to_mailbox_only_without_injecting(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "MAILBOX_ROOT", tmp_path / "mailbox")
    monkeypatch.setenv("TEAMMATE_LABEL", "sender")
    monkeypatch.setattr(server, "_resolve_target_session_id", lambda target, fallback: "SID-TARGET")
    monkeypatch.setattr(server, "osa_session_alive", lambda sid: True)

    def fail_inject(*args, **kwargs):
        raise AssertionError("default ask must not inject keystrokes")

    monkeypatch.setattr(server, "osa_clear_and_inject", fail_inject)
    monkeypatch.setattr(server, "osa_send_raw", fail_inject)

    answer = await server._ask_async("hello", target="receiver")

    assert answer == "queued mailbox-only message for receiver"
    inbox_files = list((tmp_path / "mailbox" / "receiver" / "inbox").glob("*.json"))
    assert len(inbox_files) == 1


@pytest.mark.asyncio
async def test_mailbox_only_ask_starts_watchdog(tmp_path, monkeypatch):
    calls = []
    fake_watcher = types.ModuleType("teammate_mcp.watcher")
    fake_watcher.ensure_watchdog_running = lambda: calls.append("started")
    monkeypatch.setitem(sys.modules, "teammate_mcp.watcher", fake_watcher)
    monkeypatch.setattr(server, "MAILBOX_ROOT", tmp_path / "mailbox")
    monkeypatch.setenv("TEAMMATE_LABEL", "sender")
    monkeypatch.setattr(server, "_resolve_target_session_id", lambda target, fallback: "SID-TARGET")
    monkeypatch.setattr(server, "osa_session_alive", lambda sid: True)

    await server._ask_async("hello", target="receiver", mailbox_only=True)

    assert calls == ["started"]


@pytest.mark.asyncio
async def test_injected_ask_prompt_recommends_mcp_reply_not_bash(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(server, "MAILBOX_ROOT", tmp_path / "mailbox")
    monkeypatch.setenv("TEAMMATE_LABEL", "sender")
    monkeypatch.setattr(server, "_resolve_target_session_id", lambda target, fallback: "SID-TARGET")
    monkeypatch.setattr(server, "osa_session_alive", lambda sid: True)
    monkeypatch.setattr(server, "osa_extract_compose", lambda sid: "")
    monkeypatch.setattr(server, "osa_send_raw", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "osa_capture", lambda sid: "")

    def capture_inject(sid, clear_count, body):
        captured["body"] = body

    monkeypatch.setattr(server, "osa_clear_and_inject", capture_inject)

    await server._ask_async("hello", target="receiver", mailbox_only=False)

    assert "mcp__teammate__ask(target='sender'" in captured["body"]
    assert "teammate-mcp ask sender" not in captured["body"]
