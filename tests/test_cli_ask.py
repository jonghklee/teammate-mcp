"""Unit tests for CLI ask option parsing."""

from __future__ import annotations

from teammate_mcp import cli
from teammate_mcp import server


def test_ask_mailbox_only_flag_passes_through(monkeypatch, capsys):
    captured = {}

    async def fake_ask_async(**kwargs):
        captured.update(kwargs)
        return "queued mailbox-only message for receiver"

    monkeypatch.setattr(server, "_ask_async", fake_ask_async)

    rc = cli._cmd_ask(["--mailbox-only", "receiver", "hello"])

    assert rc == 0
    assert captured["target"] == "receiver"
    assert captured["question"] == "hello"
    assert captured["mailbox_only"] is True
    assert "queued mailbox-only message for receiver" in capsys.readouterr().out


def test_ask_mailbox_only_env_passes_through(monkeypatch):
    captured = {}

    async def fake_ask_async(**kwargs):
        captured.update(kwargs)
        return "queued mailbox-only message for receiver"

    monkeypatch.setattr(server, "_ask_async", fake_ask_async)
    monkeypatch.setenv("TEAMMATE_MCP_MAILBOX_ONLY", "1")

    rc = cli._cmd_ask(["receiver", "hello"])

    assert rc == 0
    assert captured["mailbox_only"] is True


def test_ask_defaults_to_mailbox_only_without_inject_env(monkeypatch):
    captured = {}

    async def fake_ask_async(**kwargs):
        captured.update(kwargs)
        return "queued mailbox-only message for receiver"

    monkeypatch.setattr(server, "_ask_async", fake_ask_async)

    rc = cli._cmd_ask(["receiver", "hello"])

    assert rc == 0
    assert captured["mailbox_only"] is True


def test_ask_inject_env_opts_into_keystroke_delivery(monkeypatch):
    captured = {}

    async def fake_ask_async(**kwargs):
        captured.update(kwargs)
        return "sent: job_id=1 to receiver (keystroke)"

    monkeypatch.setattr(server, "_ask_async", fake_ask_async)
    monkeypatch.setenv("TEAMMATE_INJECT", "1")

    rc = cli._cmd_ask(["receiver", "hello"])

    assert rc == 0
    assert captured["mailbox_only"] is False
