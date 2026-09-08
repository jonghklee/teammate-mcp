import pytest

from teammate_mcp.codex_transport import deliver


class FakeRPC:
    def __init__(self, status="idle", direct=True):
        self.status, self.direct, self.calls = status, direct, []

    async def request(self, method, params):
        self.calls.append((method, params))
        if method == "thread/read":
            return {"thread": {"id": "thread-a", "status": {"type": self.status},
                               "canAcceptDirectInput": self.direct}}
        if method == "thread/turns/list":
            return {"data": [{"id": "turn-a", "status": "inProgress"}]}
        if method == "turn/start":
            return {"turn": {"id": "new-turn"}}
        if method == "turn/steer":
            return {"turnId": "turn-a"}
        raise AssertionError(method)


@pytest.mark.asyncio
async def test_active_thread_is_queued_without_turn_mutation():
    rpc = FakeRPC("active")
    assert await deliver(rpc, "thread-a", "message", "job-a") == {"state": "waiting"}
    assert [m for m, _ in rpc.calls] == ["thread/read"]


@pytest.mark.asyncio
async def test_idle_thread_receives_message_with_stable_client_id():
    rpc = FakeRPC()
    result = await deliver(rpc, "thread-a", "message", "job-a")
    assert result == {"state": "delivered", "turn_id": "new-turn"}
    method, params = rpc.calls[-1]
    assert method == "turn/start"
    assert params["threadId"] == "thread-a"
    assert params["clientUserMessageId"] == "teammate-job-a"
    assert params["input"][0]["text"] == "message"


@pytest.mark.asyncio
async def test_unavailable_direct_input_never_starts_a_turn():
    rpc = FakeRPC(direct=False)
    with pytest.raises(ValueError, match="direct input"):
        await deliver(rpc, "thread-a", "message", "job-a")
    assert len(rpc.calls) == 1


@pytest.mark.asyncio
async def test_explicit_immediate_policy_uses_expected_active_turn():
    rpc = FakeRPC("active")
    result = await deliver(rpc, "thread-a", "message", "job-a", policy="immediate")
    assert result["turn_id"] == "turn-a"
    assert rpc.calls[-1][0] == "turn/steer"
    assert rpc.calls[-1][1]["expectedTurnId"] == "turn-a"
