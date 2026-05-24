"""Unit tests for watchdog lifecycle and compose detection."""

from __future__ import annotations

import json
import subprocess
import time

from teammate_mcp import watcher


def _isolate_watcher_paths(tmp_path, monkeypatch):
    state = tmp_path / "run"
    monkeypatch.setattr(watcher, "STATE_DIR", state)
    monkeypatch.setattr(watcher, "PID_PATH", state / "watchdog.pid")
    monkeypatch.setattr(watcher, "HEARTBEAT_PATH", state / "watchdog-heartbeat.json")
    monkeypatch.setattr(watcher, "ENSURE_LOCK_PATH", state / "watchdog-ensure.lock")
    monkeypatch.setattr(watcher, "RUN_LOCK_PATH", state / "watchdog.lock")
    monkeypatch.setattr(watcher, "LOG", tmp_path / "logs" / "watchdog.log")


def test_watchdog_health_reports_missing_pidfile(tmp_path, monkeypatch):
    _isolate_watcher_paths(tmp_path, monkeypatch)

    ok, msg = watcher.watchdog_health()

    assert not ok
    assert "no pidfile" in msg


def test_ensure_watchdog_does_not_spawn_when_heartbeat_is_fresh(tmp_path, monkeypatch):
    _isolate_watcher_paths(tmp_path, monkeypatch)
    watcher.STATE_DIR.mkdir(parents=True)
    watcher.PID_PATH.write_text("1234", encoding="utf-8")
    watcher.HEARTBEAT_PATH.write_text(
        json.dumps({
            "pid": 1234,
            "ts": time.time(),
            "interval": 2.0,
            "wake_text": watcher.WAKE_TEXT,
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(watcher, "_pid_alive", lambda pid: True)

    def fail_popen(*args, **kwargs):
        raise AssertionError("Popen should not be called for a healthy watchdog")

    monkeypatch.setattr(watcher.subprocess, "Popen", fail_popen)

    started, msg = watcher.ensure_watchdog_running()

    assert not started
    assert "watchdog ok" in msg


def test_ensure_watchdog_spawns_when_heartbeat_is_stale(tmp_path, monkeypatch):
    _isolate_watcher_paths(tmp_path, monkeypatch)
    watcher.STATE_DIR.mkdir(parents=True)
    watcher.PID_PATH.write_text("1234", encoding="utf-8")
    watcher.HEARTBEAT_PATH.write_text(
        json.dumps({"pid": 1234, "ts": time.time() - 999, "interval": 2.0}),
        encoding="utf-8",
    )
    monkeypatch.setattr(watcher, "_pid_alive", lambda pid: True)
    captured = {}

    class FakePopen:
        pid = 7777

        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs

    monkeypatch.setattr(watcher.subprocess, "Popen", FakePopen)

    started, msg = watcher.ensure_watchdog_running(interval=2.0)

    assert started
    assert "started watchdog pid=7777" in msg
    assert captured["cmd"][-3:] == ["watch", "--interval", "2.0"]
    assert captured["kwargs"]["start_new_session"] is True
    assert captured["kwargs"]["stdin"] is subprocess.DEVNULL


def test_ensure_watchdog_spawns_when_heartbeat_uses_old_wake_text(tmp_path, monkeypatch):
    _isolate_watcher_paths(tmp_path, monkeypatch)
    watcher.STATE_DIR.mkdir(parents=True)
    watcher.PID_PATH.write_text("1234", encoding="utf-8")
    watcher.HEARTBEAT_PATH.write_text(
        json.dumps({
            "pid": 1234,
            "ts": time.time(),
            "interval": 2.0,
            "wake_text": "check inbox",
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(watcher, "_pid_alive", lambda pid: True)
    captured = {}

    class FakePopen:
        pid = 8888

        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd

    monkeypatch.setattr(watcher.subprocess, "Popen", FakePopen)

    started, msg = watcher.ensure_watchdog_running(interval=2.0)

    assert started
    assert "wake text changed" in msg
    assert captured["cmd"][-3:] == ["watch", "--interval", "2.0"]


def test_screen_compose_is_empty_accepts_empty_claude_prompt():
    screen = "\n".join([
        "previous output",
        "  ❯ ",
        "────────────────────",
        "bypass permissions on · shift+tab to cycle",
    ])

    assert watcher._screen_compose_is_empty(screen)


def test_screen_compose_is_empty_rejects_typed_prompt():
    screen = "\n".join([
        "previous output",
        "  ❯ partially typed text",
        "────────────────────",
    ])

    assert not watcher._screen_compose_is_empty(screen)


# --- starvation escape: distinguish "user typing" from "Claude working" ---

_EMPTY_PROMPT = "\n".join(["out", "  ❯ ", "────────────", "bypass permissions"])
_USER_TYPING = "\n".join(["out", "  ❯ half typed message", "────────────"])
# Claude mid-turn: no ❯ prompt visible (a spinner / tool output instead)
_CLAUDE_WORKING = "\n".join(["✻ Working… (esc to interrupt)", "  ⎿ running tool"])


def test_screen_user_is_typing_only_true_for_nonempty_prompt():
    assert watcher._screen_user_is_typing(_USER_TYPING)
    assert not watcher._screen_user_is_typing(_EMPTY_PROMPT)
    # No ❯ prompt at all = Claude working, NOT user typing.
    assert not watcher._screen_user_is_typing(_CLAUDE_WORKING)


def test_wake_action_empty_prompt_wakes_immediately():
    assert watcher._wake_action(_EMPTY_PROMPT, 0.0, 90.0) == "wake"


def test_wake_action_never_injects_while_user_typing():
    # Even past the starvation timeout, a half-typed compose is sacred.
    assert watcher._wake_action(_USER_TYPING, 999.0, 90.0) == "skip-typing"


def test_wake_action_working_pane_waits_then_force_wakes():
    # Claude busy, under timeout → hold.
    assert watcher._wake_action(_CLAUDE_WORKING, 30.0, 90.0) == "wait"
    # Waited past timeout → force-wake (the starvation escape).
    assert watcher._wake_action(_CLAUDE_WORKING, 90.0, 90.0) == "wake-starved"
    assert watcher._wake_action(_CLAUDE_WORKING, 120.0, 90.0) == "wake-starved"
