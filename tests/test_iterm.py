"""Unit tests for ANSI stripping and answer extraction.

Live iTerm tests live in test_e2e.py because they require an
interactive desktop session.
"""

from __future__ import annotations

import pytest

from teammate_mcp.iterm import extract_answer, strip_ansi


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
