"""Unit tests for ANSI stripping and answer extraction.

Live iTerm tests live in test_e2e.py because they require an
interactive desktop session.
"""

from __future__ import annotations

import pytest

from teammate_mcp import iterm
from teammate_mcp.iterm import _command_matches, extract_answer, strip_ansi


def test_strip_ansi_removes_color_codes():
    raw = "\x1b[31mhello\x1b[0m world"
    assert strip_ansi(raw) == "hello world"


def test_extract_answer_with_question_locator():
    screen = (
        "irrelevant prelude\n"
        "[teammate-mcp ASK 001 from=claude]\n"
        "What is 2+2?\n"
        "<<DONE_001>>\n"
        "(prompt)\n"
    )
    out = extract_answer(screen, "What is 2+2?", "<<DONE_001>>")
    # Whatever the model "said" between the question and the marker.
    # In this fixture the model produced no explicit prose; locator slicing
    # should still yield the empty string between question and marker.
    assert "<<DONE_001>>" not in out


def test_extract_answer_with_real_response():
    screen = (
        "[teammate-mcp ASK 002 from=claude]\n"
        "Pick a Rust crate for PDF parsing.\n"
        "Looking at popular options I would recommend `lopdf` for low-level\n"
        "control or `pdf-extract` if you only need plain text.\n"
        "<<DONE_002>>\n"
    )
    out = extract_answer(screen, "Pick a Rust crate for PDF parsing.", "<<DONE_002>>")
    assert "lopdf" in out
    assert "pdf-extract" in out
    assert "<<DONE_002>>" not in out


def test_extract_answer_marker_missing_returns_empty():
    screen = "no marker here"
    assert extract_answer(screen, "q", "<<DONE_xx>>") == ""


def test_command_matches_rejects_helper_module_false_positive():
    assert not _command_matches("python /tmp/claude-helper.py", "claude")
    assert not _command_matches("python /tmp/codex_helper.py", "codex")


def test_command_matches_accepts_real_cli_invocations():
    assert _command_matches("/opt/homebrew/bin/claude --resume", "claude")
    assert _command_matches("codex --yolo", "codex")


def test_osa_clear_and_inject_sends_body_then_standalone_carriage_return(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["script"] = cmd[2]
        return None

    monkeypatch.setattr(iterm.subprocess, "run", fake_run)

    iterm.osa_clear_and_inject("SID-1", 0, "hello")

    script = captured["script"]
    assert "write text theBody newline NO" in script
    assert "delay 0.05" in script
    assert "write text (ASCII character 13) newline NO" in script


def test_osa_send_raw_applescript_repeat_blocks_are_balanced(monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["script"] = cmd[2]
        return None

    monkeypatch.setattr(iterm.subprocess, "run", fake_run)

    iterm.osa_send_raw("SID-1", "\r")

    script = captured["script"]
    assert script.count("repeat with ") == script.count("end repeat")


# --- osa_extract_compose: NBSP padding + multi-line (ported from the
#     agent worktree) -------------------------------------------------

def test_strip_compose_padding_removes_fillers_keeps_internal():
    from teammate_mcp.iterm import _strip_compose_padding
    # trailing spaces, NBSP (U+00A0), NUL guard, tab all stripped
    assert _strip_compose_padding("hi there\xa0\xa0  \x00") == "hi there"
    assert _strip_compose_padding("done\t") == "done"
    # internal whitespace preserved
    assert _strip_compose_padding("a  b") == "a  b"
    assert _strip_compose_padding("plain") == "plain"


def _fake_screen(monkeypatch, screen: str):
    class _R:
        stdout = screen
    monkeypatch.setattr(iterm.subprocess, "run", lambda *a, **k: _R())


def test_osa_extract_compose_strips_nbsp_padding(monkeypatch):
    # Claude Code pads the compose line to the pane width with NBSP.
    # main's old rstrip(" \x00") left those behind; the ported
    # _strip_compose_padding must remove them.
    _fake_screen(monkeypatch,
        "scrollback line\n"
        "────────────────────\n"
        "❯ hello world\xa0\xa0\xa0\n"
        "────────────────────\n"
        "  [claude3] | Opus 4.7\n"
    )
    out = iterm.osa_extract_compose("SID")
    assert out == "hello world"
    assert "\xa0" not in out


def test_osa_extract_compose_multiline_joins_and_stops_at_rule(monkeypatch):
    _fake_screen(monkeypatch,
        "❯ first line\n"
        "  second line\n"
        "  third line\n"
        "────────────────────\n"
        "  ⏵⏵ bypass permissions on\n"
    )
    out = iterm.osa_extract_compose("SID")
    assert out == "first line\nsecond line\nthird line"
