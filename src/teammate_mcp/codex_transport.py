"""Codex local app-server delivery; does not read or type into terminal screens."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from websockets.legacy.client import unix_connect


class RPCError(RuntimeError):
    """The app-server explicitly rejected a request."""


class AppServer:
    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self.sequence = 0

    async def __aenter__(self):
        self.ws = await unix_connect(
            self.socket_path, uri="ws://localhost", compression=None,
            open_timeout=5, close_timeout=2, max_size=4 * 1024 * 1024,
        )
        try:
            await self.request("initialize", {
                "clientInfo": {"name": "teammate_mcp", "version": "1"},
                "capabilities": {"experimentalApi": True},
            })
            await self.ws.send(json.dumps({"method": "initialized", "params": {}}))
            return self
        except BaseException:
            await self.ws.close()
            raise

    async def __aexit__(self, *_):
        await self.ws.close()

    async def request(self, method: str, params: dict) -> dict:
        self.sequence += 1
        request_id = self.sequence
        await self.ws.send(json.dumps({"id": request_id, "method": method, "params": params}))
        async with asyncio.timeout(10):
            while True:
                message = json.loads(await self.ws.recv())
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    raise RPCError(str(message["error"]))
                return message["result"]


def default_socket() -> str:
    return str(Path.home() / ".codex" / "app-server-control" / "app-server-control.sock")


async def deliver(rpc, thread_id: str, text: str, job_id: str,
                  policy: str = "idle", before_dispatch=None) -> dict:
    if policy not in ("idle", "immediate"):
        raise ValueError("unknown delivery policy")
    thread = (await rpc.request("thread/read", {
        "threadId": thread_id, "includeTurns": False,
    }))["thread"]
    if thread.get("id") != thread_id or not thread.get("canAcceptDirectInput"):
        raise ValueError("thread does not accept direct input")
    status = thread.get("status", {}).get("type")
    if status == "active" and policy == "idle":
        return {"state": "waiting"}
    params = {"threadId": thread_id, "input": [{"type": "text", "text": text}],
              "clientUserMessageId": f"teammate-{job_id}"}
    if status == "active":
        turns = await rpc.request("thread/turns/list", {
            "threadId": thread_id, "limit": 1, "sortDirection": "desc", "itemsView": "notLoaded",
        })
        active = next((t for t in turns.get("data", []) if t.get("status") == "inProgress"), None)
        if not active:
            return {"state": "waiting"}
        params["expectedTurnId"] = active["id"]
        method = "turn/steer"
    elif status == "idle":
        method = "turn/start"
    else:
        raise ValueError(f"thread is not available: {status}")
    if before_dispatch:
        before_dispatch()
    response = await rpc.request(method, params)
    turn_id = response.get("turnId") if method == "turn/steer" else response.get("turn", {}).get("id")
    if not turn_id:
        raise RuntimeError("app-server accepted request without a turn id")
    return {"state": "delivered", "turn_id": turn_id}
