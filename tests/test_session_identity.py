import asyncio
from types import SimpleNamespace
from contextvars import ContextVar

import pytest
from teammate_mcp import server, registry, session_identity as identity


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "registry.json")
    monkeypatch.setattr(registry, "LOCK_PATH", tmp_path / "registry.lock")
    monkeypatch.setattr(server, "MAILBOX_ROOT", tmp_path / "mailbox")
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.setattr(server, "_startup_pane", None)


def test_request_thread_takes_precedence_and_resets(monkeypatch):
    monkeypatch.setenv("CODEX_THREAD_ID", "environment-thread")
    with identity.request_identity({"threadId": "actual-thread"}):
        assert identity.current_thread_id() == "actual-thread"
    assert identity.current_thread_id() == "environment-thread"


@pytest.mark.asyncio
async def test_concurrent_requests_do_not_mix_identities():
    async def one(thread_id):
        with identity.request_identity({"threadId": thread_id}):
            await asyncio.sleep(0)
            return identity.current_thread_id()
    assert await asyncio.gather(one("thread-a"), one("thread-b")) == ["thread-a", "thread-b"]


@pytest.mark.asyncio
async def test_first_call_bootstraps_and_ignores_stale_pane_label(monkeypatch):
    monkeypatch.setenv("TEAMMATE_LABEL", "stale-pane")
    async def enable(label, policy="idle", enabled=True):
        return {"enabled": True}
    monkeypatch.setattr(server, "configure_mailbox_delivery", enable)
    with identity.request_identity({"threadId": "thread-a"}):
        await server._bootstrap_caller()
        assert server._caller_label() == "codex-thread-a"
        assert await server.inbox() == []
        await server._ask_async("self", target="codex-thread-a")
        assert (await server.inbox())[0]["from_"] == "codex-thread-a"


@pytest.mark.asyncio
async def test_bootstrap_preserves_explicit_disabled_delivery(monkeypatch):
    await server.register_mailbox("named", "thread-a")
    with registry._exclusive_lock():
        data = registry.load(); data["named"]["auto_delivery"] = False; registry._save_raw(data)
    async def unexpected(*args, **kwargs):
        raise AssertionError("must not re-enable an explicitly disabled endpoint")
    monkeypatch.setattr(server, "configure_mailbox_delivery", unexpected)
    with identity.request_identity({"threadId": "thread-a"}):
        await server._bootstrap_caller()
        assert server._caller_label() == "named"


@pytest.mark.asyncio
async def test_cli_thread_keeps_its_verified_pane_when_no_appserver_transport(monkeypatch):
    registry.register("codex1", "ACTUAL-PANE", 1, "codex")
    monkeypatch.setattr(server, "_startup_pane", {"label": "codex1", "session_id": "ACTUAL-PANE"})
    from teammate_mcp import codex_transport
    class Unavailable:
        def __init__(self, *args): pass
        async def __aenter__(self): raise OSError("no local app-server")
        async def __aexit__(self, *args): pass
    monkeypatch.setattr(codex_transport, "AppServer", Unavailable)
    with identity.request_identity({"threadId": "cli-thread"}):
        await server._bootstrap_caller()
        assert server._caller_label() == "codex1"
        assert registry.lookup("codex1")["thread_id"] == "cli-thread"
        assert registry.lookup("codex1")["session_id"] == "ACTUAL-PANE"
        assert list(registry.all_labels()) == ["codex1"]
