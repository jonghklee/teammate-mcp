import pytest
from teammate_mcp import server, mailbox_worker as worker


@pytest.fixture
def message(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "MAILBOX_ROOT", tmp_path)
    return server._write_inbox("receiver", {"job_id": "job-a", "from_": "sender",
        "to": "receiver", "body": "question", "recipient_thread_id": "thread-a"})


@pytest.mark.asyncio
async def test_worker_never_redelivers_after_acceptance(message):
    calls = []
    async def send(*args, **kwargs):
        calls.append(args)
        kwargs["before_dispatch"]()
        return {"state": "delivered", "turn_id": "turn-a"}
    await worker.deliver_record("receiver", message, {"thread_id": "thread-a"}, None, send)
    await worker.deliver_record("receiver", message, {"thread_id": "thread-a"}, None, send)
    assert len(calls) == 1
    assert message.exists(), "Delivery must not erase the question"


@pytest.mark.asyncio
async def test_ambiguous_dispatch_is_not_blindly_retried(message):
    calls = []
    async def send(*args, **kwargs):
        calls.append(args)
        kwargs["before_dispatch"]()
        raise TimeoutError("connection lost after write")
    await worker.deliver_record("receiver", message, {"thread_id": "thread-a"}, None, send)
    await worker.deliver_record("receiver", message, {"thread_id": "thread-a"}, None, send)
    assert len(calls) == 1
    assert worker.read_delivery("receiver", "job-a")["state"] == "uncertain"


@pytest.mark.asyncio
async def test_busy_delivery_remains_retryable(message):
    async def send(*args, **kwargs):
        return {"state": "waiting"}
    await worker.deliver_record("receiver", message, {"thread_id": "thread-a"}, None, send)
    assert worker.read_delivery("receiver", "job-a")["state"] == "waiting"


@pytest.mark.asyncio
async def test_reused_thread_does_not_receive_old_question(message):
    async def send(*args, **kwargs):
        raise AssertionError("wrong owner must not receive the message")
    await worker.deliver_record("receiver", message, {"thread_id": "other-thread"}, None, send)
    assert worker.read_delivery("receiver", "job-a")["state"] == "failed"


@pytest.mark.asyncio
async def test_failure_before_dispatch_is_retryable(message):
    async def send(*args, **kwargs):
        raise TimeoutError("read-only query timed out")
    await worker.deliver_record("receiver", message, {"thread_id": "thread-a"}, None, send)
    assert worker.read_delivery("receiver", "job-a")["state"] == "retry"


@pytest.mark.asyncio
async def test_manual_processing_while_probing_cancels_delivery(message):
    async def send(*args, **kwargs):
        await server.mark_processed("job-a", "receiver", "handled manually")
        kwargs["before_dispatch"]()
        raise AssertionError("must not dispatch a processed message")
    await worker.deliver_record("receiver", message, {"thread_id": "thread-a"}, None, send)
    assert worker.read_delivery("receiver", "job-a")["state"] == "processed"
