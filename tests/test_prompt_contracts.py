from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_ask_slash_command_uses_mcp_tool_not_bash_cli():
    text = (ROOT / "commands" / "ask.md").read_text(encoding="utf-8")

    assert "Call `mcp__teammate__ask`" in text
    assert "do NOT call `mcp__teammate__ask`" not in text
    assert "Run this exact Bash command" not in text


def test_claude_template_prefers_mcp_tool_for_teammate_messages():
    text = (ROOT / "templates" / "CLAUDE.md").read_text(encoding="utf-8")

    assert "**Preferred path — MCP tool:**" in text
    assert "**Preferred path — Bash CLI" not in text
    assert "teammate-mcp ask <sender>" not in text


def test_inbox_drain_hook_recommends_mcp_reply_only(tmp_path, monkeypatch, capsys):
    hook_path = ROOT / "hooks" / "user_prompt_submit_inbox_drain.py"
    spec = importlib.util.spec_from_file_location("hook_drain_under_test", hook_path)
    assert spec is not None
    hook = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(hook)

    monkeypatch.setenv("TEAMMATE_LABEL", "receiver")
    monkeypatch.setattr(hook, "MAILBOX", tmp_path / "mailbox")
    monkeypatch.setattr(hook, "LOG", tmp_path / "logs" / "hook.log")

    inbox = tmp_path / "mailbox" / "receiver" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "job-1.json").write_text(
        json.dumps({
            "job_id": "job-1",
            "from_": "sender",
            "to": "receiver",
            "body": "hello",
        }),
        encoding="utf-8",
    )

    assert hook.main() == 0

    out = capsys.readouterr().out
    assert "mcp__teammate__reply(job_id='job-1'" in out
    assert "teammate-mcp ask sender" not in out
    assert "via Bash" not in out
