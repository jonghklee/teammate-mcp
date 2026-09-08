import json
import os
import time
from contextlib import contextmanager

import pytest
from teammate_mcp import pane_delivery, server, registry, watcher


@pytest.fixture(autouse=True)
def isolated_log(tmp_path, monkeypatch):
    monkeypatch.setattr(watcher, "LOG", tmp_path / "watchdog.log")


def test_live_lease_defers_delivery_but_expired_lease_does_not():
    record = {"delivery_lease": {"pid": os.getpid(), "expires_at": time.time() + 30}}
    assert pane_delivery.lease_active(record)
    record["delivery_lease"]["expires_at"] = 0
    assert not pane_delivery.lease_active(record)


def test_watchdog_sends_actual_pending_message_not_dot(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "MAILBOX_ROOT", tmp_path)
    monkeypatch.setattr(watcher, "MAILBOX", tmp_path)
    monkeypatch.setattr(registry, "lookup", lambda label: {"session_id": "SID"})
    from teammate_mcp import iterm
    monkeypatch.setattr(iterm, "osa_capture", lambda sid: "❯ \n")
    sent = []
    monkeypatch.setattr(iterm, "osa_clear_and_inject", lambda sid, n, body: sent.append(body))
    source = server._write_inbox("receiver", {"job_id": "original-job", "from_": "sender",
        "to": "receiver", "body": "actual question", "delivery_mode": "mailbox"})
    assert watcher._wake("SID", "receiver")
    assert len(sent) == 1 and "actual question" in sent[0] and "original-job" in sent[0]
    assert sent[0].strip() != "."
    assert not source.exists()


def test_watchdog_does_not_duplicate_direct_injection(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "MAILBOX_ROOT", tmp_path)
    monkeypatch.setattr(watcher, "MAILBOX", tmp_path)
    monkeypatch.setattr(registry, "lookup", lambda label: {"session_id": "SID"})
    server._write_inbox("receiver", {"job_id": "job", "body": "question",
        "delivery_lease": {"pid": os.getpid(), "expires_at": time.time() + 30}})
    assert not watcher._wake("SID", "receiver")


def test_watchdog_rechecks_disappeared_candidate(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "MAILBOX_ROOT", tmp_path)
    monkeypatch.setattr(watcher, "MAILBOX", tmp_path)
    monkeypatch.setattr(registry, "lookup", lambda label: {"session_id": "SID"})
    assert not watcher._wake("SID", "receiver")


def test_post_send_capture_failure_never_replays_message(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "MAILBOX_ROOT", tmp_path)
    monkeypatch.setattr(watcher, "MAILBOX", tmp_path)
    monkeypatch.setattr(registry, "lookup", lambda label: {"session_id": "SID"})
    from teammate_mcp import iterm
    reads = 0
    def capture(sid):
        nonlocal reads
        reads += 1
        if reads == 2: raise TimeoutError("capture after send timed out")
        return "❯ \n"
    monkeypatch.setattr(iterm, "osa_capture", capture)
    sent = []
    monkeypatch.setattr(iterm, "osa_clear_and_inject", lambda sid, n, body: sent.append(body))
    server._write_inbox("receiver", {"job_id": "job", "from_": "sender", "body": "question", "delivery_mode": "mailbox"})
    assert not watcher._wake("SID", "receiver")
    assert not watcher._wake("SID", "receiver")
    assert len(sent) == 1


@pytest.fixture(autouse=True)
def explicit_legacy_transport(monkeypatch):
    monkeypatch.setenv("TEAMMATE_LEGACY_PANE_INPUT", "1")
