"""Unit tests for the file-system FIFO queue."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from teammate_mcp.queue import MessageQueue


def test_enqueue_creates_pending_file(tmp_path):
    q = MessageQueue(mode="audit", base=tmp_path)
    msg = q.enqueue("claude", "codex", "hello?")
    pending = list((tmp_path / "pending").glob("*.json"))
    assert len(pending) == 1
    assert pending[0].stem == msg.id
    payload = json.loads(pending[0].read_text())
    assert payload["question"] == "hello?"
    assert payload["from_agent"] == "claude"
    assert payload["to_agent"] == "codex"


def test_claim_atomic_single_winner(tmp_path):
    q = MessageQueue(mode="audit", base=tmp_path)
    msg = q.enqueue("claude", "codex", "race?")
    first = q.claim(msg.id)
    second = q.claim(msg.id)
    assert first is not None
    assert second is None
    assert (tmp_path / "inflight" / f"{msg.id}.json").exists()
    assert not (tmp_path / "pending" / f"{msg.id}.json").exists()


def test_complete_moves_inflight_to_done(tmp_path):
    q = MessageQueue(mode="audit", base=tmp_path)
    msg = q.enqueue("claude", "codex", "ping")
    q.claim(msg.id)
    q.complete(msg.id, "pong")
    assert (tmp_path / "done" / f"{msg.id}.json").exists()
    assert (tmp_path / "done" / f"{msg.id}.answer.txt").read_text() == "pong"
    assert not (tmp_path / "inflight" / f"{msg.id}.json").exists()


def test_fail_records_reason(tmp_path):
    q = MessageQueue(mode="audit", base=tmp_path)
    msg = q.enqueue("claude", "codex", "boom")
    q.claim(msg.id)
    q.fail(msg.id, "timeout")
    failed_path = tmp_path / "failed" / f"{msg.id}.json"
    assert failed_path.exists()
    payload = json.loads(failed_path.read_text())
    assert payload["failed_reason"] == "timeout"


def test_fifo_order_preserved(tmp_path):
    q = MessageQueue(mode="audit", base=tmp_path)
    ids = [q.enqueue("claude", "codex", f"q{i}").id for i in range(5)]
    pending_sorted = sorted(p.stem for p in (tmp_path / "pending").glob("*.json"))
    assert pending_sorted == sorted(ids)


def test_status_counts(tmp_path):
    q = MessageQueue(mode="audit", base=tmp_path)
    q.enqueue("claude", "codex", "a")
    msg = q.enqueue("claude", "codex", "b")
    q.claim(msg.id)
    q.complete(msg.id, "answer")

    status = q.status()
    assert status["mode"] == "audit"
    assert status["pending"] == 1
    assert status["inflight"] == 0
    assert status["done"] == 1
    assert status["failed"] == 0


def test_ephemeral_mode_uses_tempdir():
    q = MessageQueue(mode="ephemeral")
    assert q.base.exists()
    assert "teammate-mcp-" in str(q.base)
    # Running second instance should pick a different tempdir
    q2 = MessageQueue(mode="ephemeral")
    assert q2.base != q.base


def test_invalid_mode_rejected():
    with pytest.raises(ValueError):
        MessageQueue(mode="bogus")
