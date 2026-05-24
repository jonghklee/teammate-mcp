"""Tests for label classification and auto-label selection."""

from __future__ import annotations

from teammate_mcp import server


def test_classify_prefers_codex_job_over_stale_claude_session_name():
    assert server._classify("codex", "Claude Code") == "codex"


def test_classify_prefers_claude_job_over_stale_codex_session_name():
    assert server._classify("claude", "Codex") == "claude"


def test_classify_uses_session_name_when_job_is_uninformative():
    assert server._classify("Python", "Claude Code") == "claude"
    assert server._classify("zsh", "codex worker") == "codex"
