"""Pane-free sessions use explicit mailboxes, never invented iTerm IDs."""
import pytest

from teammate_mcp import registry, server


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "registry.json")
    monkeypatch.setattr(registry, "LOCK_PATH", tmp_path / "registry.lock")
    monkeypatch.setattr(server, "MAILBOX_ROOT", tmp_path / "mailbox")
    monkeypatch.setenv("CODEX_THREAD_ID", "test-thread")
    monkeypatch.setenv("TEAMMATE_LABEL", "sender")


@pytest.mark.asyncio
async def test_register_mailbox_has_no_pane_and_cannot_overwrite_another_owner():
    assert (await server.register_mailbox("codex-test")).startswith("registered")
    rec = registry.lookup("codex-test")
    assert rec["session_id"] == ""
    assert rec["transport"] == "mailbox"
    assert rec["thread_id"] == "test-thread"
    registry.register("claude39", "real-pane", 1, "claude")
    assert (await server.register_mailbox("claude39")).startswith("ERROR:")
    assert registry.lookup("claude39")["session_id"] == "real-pane"


@pytest.mark.asyncio
async def test_register_mailbox_rejects_path_labels():
    assert (await server.register_mailbox("../escape")).startswith("ERROR:")


@pytest.mark.asyncio
async def test_registered_mailbox_receives_without_iterm(monkeypatch):
    registry.register("receiver", "", 1, "codex", extra={"transport": "mailbox"})
    monkeypatch.setenv("TEAMMATE_LABEL", "claude39")
    def no_iterm(*args, **kwargs):
        raise AssertionError("A mailbox endpoint must not query or inject iTerm")
    monkeypatch.setattr(server, "osa_session_alive", no_iterm)
    monkeypatch.setattr(server, "osa_clear_and_inject", no_iterm)
    result = await server._ask_async("Direct question", target="receiver")
    assert result.startswith("queued mailbox")
    messages = await server.inbox("receiver")
    assert [(m["from_"], m["body"]) for m in messages] == [("claude39", "Direct question")]


@pytest.mark.asyncio
async def test_mailbox_write_failure_is_reported(monkeypatch):
    registry.register("receiver", "", 1, "codex", extra={"transport": "mailbox"})
    def fail_write(*args):
        raise OSError("disk full")
    monkeypatch.setattr(server, "_write_inbox", fail_write)
    result = await server._ask_async("Direct question", target="receiver")
    assert result.startswith("ERROR:") and "disk full" in result


@pytest.mark.asyncio
async def test_thread_identity_supplies_sender_and_default_inbox(monkeypatch):
    monkeypatch.delenv("TEAMMATE_LABEL", raising=False)
    monkeypatch.delenv("TERM_SESSION_ID", raising=False)
    await server.register_mailbox("codex-test")
    result = await server._ask_async("loopback", target="codex-test")
    assert result.startswith("queued mailbox")
    messages = await server.inbox()
    assert len(messages) == 1 and messages[0]["from_"] == "codex-test"


@pytest.mark.asyncio
async def test_empty_pane_id_does_not_claim_an_unrelated_sender(monkeypatch):
    registry.register("someone-else", "", 1, "codex", extra={"transport": "mailbox"})
    monkeypatch.delenv("TEAMMATE_LABEL", raising=False)
    monkeypatch.setenv("TERM_SESSION_ID", "w0:REAL-PANE")
    assert (await server._ask_async("hello", target="someone-else")).startswith("ERROR:")
    assert await server.inbox("someone-else") == []
    assert await server.inbox() == [{"error": "no caller label resolvable"}]


@pytest.mark.asyncio
async def test_mailbox_registration_requires_owner_and_preserves_other_thread(monkeypatch):
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    assert (await server.register_mailbox("codex-test")).startswith("ERROR:")
    await server.register_mailbox("codex-test", "owner-a")
    assert (await server.register_mailbox("codex-test", "owner-b")).startswith("ERROR:")
    assert registry.lookup("codex-test")["thread_id"] == "owner-a"


@pytest.mark.asyncio
async def test_pane_registration_cannot_replace_mailbox():
    await server.register_mailbox("codex-test")
    with pytest.raises(ValueError):
        registry.register("codex-test", "pane", 1, "claude")
    assert registry.lookup("codex-test")["transport"] == "mailbox"


@pytest.mark.asyncio
async def test_thread_cannot_register_ambiguous_alias():
    await server.register_mailbox("codex-test")
    assert (await server.register_mailbox("second-name")).startswith("ERROR:")
    assert server._caller_mailbox_label() == "codex-test"


@pytest.mark.asyncio
async def test_unregister_mailbox_archives_previous_owners_messages():
    await server.register_mailbox("codex-test", "owner-a")
    await server._ask_async("private-to-owner-a", target="codex-test")
    registry.unregister("codex-test")
    await server.register_mailbox("codex-test", "owner-b")
    assert await server.inbox("codex-test") == []


@pytest.mark.asyncio
async def test_reregister_keeps_delivery_configuration():
    await server.register_mailbox("codex-test")
    with registry._exclusive_lock():
        data = registry.load()
        data["codex-test"].update(auto_delivery=True, delivery_policy="idle")
        registry._save_raw(data)
    await server.register_mailbox("codex-test")
    assert registry.lookup("codex-test")["auto_delivery"] is True


@pytest.mark.asyncio
async def test_automatic_registration_reuses_current_threads_address():
    await server.register_mailbox("codex-test")
    assert "'codex-test'" in await server.register_mailbox()
    assert list(registry.all_labels()) == ["codex-test"]
