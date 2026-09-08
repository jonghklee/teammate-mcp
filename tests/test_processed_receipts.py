"""Replies must survive the eager removal of keystroke-delivered inboxes."""

import json

import pytest

from teammate_mcp import server


@pytest.mark.asyncio
async def test_reply_is_saved_after_keystroke_delivery_removed_inbox(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "MAILBOX_ROOT", tmp_path)
    result = await server.mark_processed("delivered-job", "claude39", "수신 확인")
    receipt = tmp_path / "claude39" / "processed" / "delivered-job.json"
    assert result.startswith("ok:")
    assert receipt.exists(), "Successful completion must persist the reply"
    data = json.loads(receipt.read_text())
    assert data["job_id"] == "delivered-job"
    assert data["terminal"]["reply"] == "수신 확인"
    assert "body" not in data  # Do not invent the deleted message.


@pytest.mark.asyncio
async def test_reply_updates_previously_drained_record_without_losing_message(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "MAILBOX_ROOT", tmp_path)
    server._write_inbox("claude39", {"job_id": "drained-job", "body": "hello"})
    server._move_to_processed("claude39", "drained-job", {"status": "drained"})
    await server.mark_processed("drained-job", "claude39", "hello back")
    data = json.loads((tmp_path / "claude39" / "processed" / "drained-job.json").read_text())
    assert data["body"] == "hello"
    assert data["terminal"]["reply"] == "hello back"


@pytest.mark.asyncio
async def test_normal_completion_preserves_message_and_removes_inbox(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "MAILBOX_ROOT", tmp_path)
    source = server._write_inbox("claude39", {"job_id": "queued-job", "body": "hello"})
    await server.mark_processed("queued-job", "claude39", "received")
    data = json.loads((tmp_path / "claude39" / "processed" / "queued-job.json").read_text())
    assert data["body"] == "hello"
    assert data["terminal"]["reply"] == "received"
    assert not source.exists()


@pytest.mark.asyncio
async def test_late_drain_and_empty_ack_do_not_erase_completed_reply(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "MAILBOX_ROOT", tmp_path)
    await server.mark_processed("job", "claude39", "actual answer")
    server._move_to_processed("claude39", "job", {"status": "drained"})
    await server.mark_processed("job", "claude39", "")
    data = json.loads((tmp_path / "claude39" / "processed" / "job.json").read_text())
    assert data["terminal"]["reply"] == "actual answer"


@pytest.mark.asyncio
async def test_receipt_rejects_paths_outside_mailbox(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "MAILBOX_ROOT", tmp_path)
    assert (await server.mark_processed("../outside", "claude39", "bad")).startswith("ERROR:")
    assert not list(tmp_path.rglob("*.json"))
