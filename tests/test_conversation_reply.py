import json
import pytest
from teammate_mcp import server, registry


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "MAILBOX_ROOT", tmp_path / "mailbox")
    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "registry.json")
    monkeypatch.setattr(registry, "LOCK_PATH", tmp_path / "registry.lock")
    monkeypatch.setenv("TEAMMATE_LABEL", "receiver")
    registry.register_mailbox("sender", "sender-thread", "/tmp")
    registry.register_mailbox("receiver", "receiver-thread", "/tmp")


@pytest.mark.asyncio
async def test_reply_routes_to_original_sender_and_keeps_question_link():
    source = server._write_inbox("receiver", {"job_id": "question-a", "from_": "sender",
        "to": "receiver", "body": "question", "conversation_id": "conversation-a"})
    source.unlink()  # Existing keystroke delivery removes the inbox.
    result = await server.reply("question-a", "answer")
    assert result.startswith("queued mailbox")
    messages = await server.inbox("sender")
    assert len(messages) == 1
    assert messages[0]["from_"] == "receiver"
    assert messages[0]["in_reply_to"] == "question-a"
    assert messages[0]["conversation_id"] == "conversation-a"
    assert messages[0]["message_kind"] == "reply"
    assert messages[0]["body"] == "answer"


@pytest.mark.asyncio
async def test_duplicate_reply_is_not_sent_twice():
    server._write_inbox("receiver", {"job_id": "question-a", "from_": "sender", "body": "q"})
    await server.reply("question-a", "answer")
    result = await server.reply("question-a", "answer")
    assert result.startswith("already replied")
    assert len(await server.inbox("sender")) == 1


@pytest.mark.asyncio
async def test_missing_question_never_guesses_a_recipient():
    assert (await server.reply("missing", "answer")).startswith("ERROR:")
    assert await server.inbox("sender") == []


@pytest.mark.asyncio
async def test_retry_repairs_receipt_after_answer_was_sent(monkeypatch):
    source = server._write_inbox("receiver", {"job_id": "question-a", "from_": "sender", "body": "q"})
    actual = server._move_to_processed
    def fail_receipt(*args):
        raise OSError("receipt disk write failed")
    monkeypatch.setattr(server, "_move_to_processed", fail_receipt)
    with pytest.raises(OSError):
        await server.reply("question-a", "answer")
    monkeypatch.setattr(server, "_move_to_processed", actual)
    await server.reply("question-a", "answer")
    assert not source.exists()
    assert len(await server.inbox("sender")) == 1
