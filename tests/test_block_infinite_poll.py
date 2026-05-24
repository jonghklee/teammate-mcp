"""Tests for Claude Bash PreToolUse infinite polling guard."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "hooks" / "block_infinite_poll.py"


def _load_hook():
    spec = importlib.util.spec_from_file_location("block_infinite_poll", HOOK)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_blocks_dynamic_now_plus_timeout_until_loop():
    hook = _load_hook()
    command = (
        'until [ "$(date +%s)" -gt "$(($(date +%s) + 25))" ] '
        '|| teammate-mcp drain 2>&1 | grep -q "ASK"; do sleep 2; done'
    )

    decision = hook.evaluate_command(command)

    assert decision.blocked
    assert "dynamic timeout" in decision.reason


def test_blocks_unbounded_while_sleep_polling_loop():
    hook = _load_hook()
    command = 'while true; do teammate-mcp drain | grep -q ASK && break; sleep 2; done'

    decision = hook.evaluate_command(command)

    assert decision.blocked
    assert "unbounded polling loop" in decision.reason


def test_allows_bounded_for_loop_with_sleep():
    hook = _load_hook()
    command = 'for i in 1 2 3; do teammate-mcp drain || true; sleep 1; done'

    decision = hook.evaluate_command(command)

    assert not decision.blocked


def test_main_blocks_bash_hook_payload(capsys, monkeypatch):
    hook = _load_hook()
    payload = json.dumps({
        "tool_name": "Bash",
        "tool_input": {
            "command": "while :; do teammate-mcp drain; sleep 2; done",
        },
    })
    monkeypatch.setattr("sys.stdin", type("FakeIn", (), {"read": lambda self: payload})())

    rc = hook.main()

    captured = capsys.readouterr()
    assert rc == 2
    assert "Blocked teammate-mcp polling loop" in captured.err
