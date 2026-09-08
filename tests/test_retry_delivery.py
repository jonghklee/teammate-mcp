import pytest
from teammate_mcp import server, registry, mailbox_worker


@pytest.mark.asyncio
async def test_retries_known_failure_but_never_uncertain_delivery(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "MAILBOX_ROOT", tmp_path)
    monkeypatch.setattr(server, "_caller_label", lambda: "receiver")
    monkeypatch.setattr(registry, "lookup", lambda label: {"thread_id": "thread-a"})
    server._write_inbox("receiver", {"job_id": "job", "body": "q", "recipient_thread_id": "thread-a"})
    mailbox_worker._write_state("receiver", "job", {"state": "failed", "error": "offline"})
    assert (await server.retry_delivery("job"))["state"] == "queued"
    mailbox_worker._write_state("receiver", "job", {"state": "uncertain"})
    assert "error" in await server.retry_delivery("job")
    assert mailbox_worker.read_delivery("receiver", "job")["state"] == "uncertain"
