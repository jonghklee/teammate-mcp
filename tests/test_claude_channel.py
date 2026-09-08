import json
import os
import time
import pytest
from teammate_mcp import registry, server, claude_channel


class FakeSession:
    def __init__(self): self.notifications = []
    async def send_notification(self, notification):
        self.notifications.append(notification.model_dump(exclude_none=True))


@pytest.fixture
def channel(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "MAILBOX_ROOT", tmp_path / "mailbox")
    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "registry.json")
    monkeypatch.setattr(registry, "LOCK_PATH", tmp_path / "registry.lock")
    registry.register("claude-test", "pane-id", 1, "claude")
    return claude_channel.ChannelPump("claude-test", FakeSession())


@pytest.mark.asyncio
async def test_channel_requires_receiver_handshake_before_messages(channel):
    await channel.connect()
    assert len(channel.session.notifications) == 1
    assert channel.session.notifications[0]["method"] == "notifications/claude/channel"
    server._write_inbox("claude-test", {"job_id": "job", "from_": "sender", "body": "question"})
    await channel.scan_once()
    assert len(channel.session.notifications) == 1
    assert registry.lookup("claude-test")["channel"]["state"] == "awaiting_handshake"


@pytest.mark.asyncio
async def test_channel_pushes_without_keyboard_and_deduplicates(channel, monkeypatch):
    def forbidden(*args, **kwargs): raise AssertionError("must not touch terminal input")
    monkeypatch.setattr(server, "osa_clear_and_inject", forbidden)
    monkeypatch.setattr(server, "osa_send_raw", forbidden)
    await channel.connect()
    assert channel.confirm(channel.nonce)["ready"]
    server._write_inbox("claude-test", {"job_id": "job", "from_": "sender", "body": "native question"})
    await channel.scan_once(); await channel.scan_once()
    assert len(channel.session.notifications) == 2
    assert "native question" in channel.session.notifications[1]["params"]["content"]
    assert channel.session.notifications[1]["params"]["meta"]["job_id"] == "job"
    assert (server.MAILBOX_ROOT / "claude-test" / "inbox" / "job.json").exists()


@pytest.mark.asyncio
async def test_wrong_handshake_never_marks_channel_ready(channel):
    await channel.connect()
    assert "error" in channel.confirm("wrong")
    assert not channel.ready


@pytest.mark.asyncio
async def test_closing_old_channel_does_not_disable_new_connection(channel):
    await channel.connect()
    newer = claude_channel.ChannelPump(channel.label, FakeSession())
    await newer.connect(); newer.confirm(newer.nonce)
    await channel.close()
    assert registry.lookup(channel.label)["channel"]["connection_id"] == newer.connection_id
    assert registry.lookup(channel.label)["channel"]["state"] == "ready"


@pytest.mark.asyncio
async def test_native_default_queues_without_reading_or_writing_compose(channel, monkeypatch):
    monkeypatch.delenv("TEAMMATE_LEGACY_PANE_INPUT", raising=False)
    monkeypatch.setenv("TEAMMATE_LABEL", "sender")
    def forbidden(*args, **kwargs): raise AssertionError("compose must not be inspected")
    monkeypatch.setattr(server, "osa_session_alive", forbidden)
    monkeypatch.setattr(server, "osa_extract_compose", forbidden)
    result = await server._ask_async("hello", target=channel.label)
    assert "channel" in result and "job_id=" in result
    assert len(await server.inbox(channel.label)) == 1


@pytest.mark.asyncio
async def test_fast_receipt_is_not_recreated_by_sender(channel):
    await channel.connect(); channel.confirm(channel.nonce)
    server._write_inbox(channel.label, {"job_id": "job", "from_": "sender", "body": "q"})
    original = channel.session.send_notification
    async def immediate_receipt(notification):
        await original(notification)
        await server.mark_processed("job", channel.label, "received")
    channel.session.send_notification = immediate_receipt
    await channel.scan_once()
    assert not (server.MAILBOX_ROOT / channel.label / "inbox" / "job.json").exists()


@pytest.mark.asyncio
async def test_helper_reregistration_does_not_destroy_channel(channel):
    await channel.connect(); channel.confirm(channel.nonce)
    registry.register(channel.label, "pane-id", 2, "claude")
    assert registry.lookup(channel.label)["channel"]["connection_id"] == channel.connection_id


@pytest.mark.asyncio
async def test_channel_does_not_race_an_active_legacy_sender(channel):
    await channel.connect(); channel.confirm(channel.nonce)
    server._write_inbox(channel.label, {"job_id": "job", "from_": "sender", "body": "q",
        "delivery_lease": {"pid": os.getpid(), "expires_at": time.time() + 60}})
    await channel.scan_once()
    assert len(channel.session.notifications) == 1


def test_channel_owner_cannot_be_replaced_by_another_pane(channel):
    registry.register("headless", "", 1, "claude",
                      extra={"transport": "claude-channel", "claude_owner": "owner-a"})
    with pytest.raises(ValueError):
        registry.register("headless", "different-pane", 2, "claude")
    assert registry.lookup("headless")["claude_owner"] == "owner-a"
