"""Claude-native MCP notifications. Never reads or writes terminal input."""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
import uuid
import fcntl

from mcp.types import Notification
from pydantic import BaseModel

from . import registry


class ChannelParams(BaseModel):
    content: str
    meta: dict[str, str]


class ChannelPump:
    def __init__(self, label, session):
        self.label, self.session = label, session
        self.connection_id = uuid.uuid4().hex
        self.nonce = secrets.token_urlsafe(24)
        self.ready = False
        self.task = None

    def _current(self):
        record = registry.lookup(self.label) or {}
        return record.get("channel", {}).get("connection_id") == self.connection_id

    def _state(self, state):
        with registry._exclusive_lock():
            data = registry.load()
            record = data.get(self.label)
            if not record:
                return False
            current = record.get("channel", {})
            if current.get("connection_id") not in (None, self.connection_id):
                return False
            record["channel"] = {"connection_id": self.connection_id, "state": state,
                                 "pid": os.getpid(), "last_seen": time.time()}
            registry._save_raw(data)
            return True

    async def _notify(self, content, meta):
        # The Claude extension is a standard JSON-RPC notification with a
        # custom method. No keyboard, AppleScript, or screen transport exists here.
        await self.session.send_notification(Notification[ChannelParams, str](
            method="notifications/claude/channel",
            params=ChannelParams(content=content, meta=meta),
        ))

    async def connect(self):
        with registry._exclusive_lock():
            data = registry.load()
            if self.label not in data:
                raise ValueError("channel requires a registered session")
            data[self.label]["channel"] = {"connection_id": self.connection_id,
                "state": "awaiting_handshake", "pid": os.getpid(), "last_seen": time.time()}
            registry._save_raw(data)
        await self._notify(
            "Teammate channel transport handshake. Call the channel_ready tool with "
            f"nonce={self.nonce!r}. This confirms this session actually receives native "
            "channel events. Do not type into the terminal or change the user's draft.",
            {"kind": "handshake", "recipient": self.label},
        )

    def confirm(self, nonce):
        if not secrets.compare_digest(nonce, self.nonce) or not self._current():
            return {"error": "invalid or stale channel handshake"}
        self.ready = True
        self._state("ready")
        return {"ready": True, "label": self.label, "transport": "claude-channel"}

    async def scan_once(self):
        if not self.ready or not self._current():
            return
        from . import server
        from .pane_delivery import lease_active
        for path in sorted(server._mailbox_dir(self.label, "inbox").glob("*.json")):
            if not self._current():
                return
            try:
                record = json.loads(path.read_text())
            except FileNotFoundError:
                continue
            if lease_active(record):
                continue
            if record.get("channel_delivery", {}).get("state") in ("dispatching", "sent", "uncertain", "failed"):
                continue
            if record.get("pane_delivery", {}).get("state") in ("dispatching", "submitted", "uncertain"):
                continue
            endpoint = registry.lookup(self.label) or {}
            owner = record.get("recipient_session_id")
            if owner and owner != endpoint.get("session_id"):
                continue
            native_owner = record.get("recipient_claude_owner")
            if native_owner and native_owner != endpoint.get("claude_owner"):
                self._update_pending(path, {"state": "failed", "error": "channel recipient owner changed"})
                continue
            jid = record["job_id"]
            record["delivery_mode"] = "claude-channel"
            delivery = {"state": "dispatching", "connection_id": self.connection_id}
            if not self._update_pending(path, delivery):
                continue
            try:
                await self._notify(
                    record["body"] + "\n\nPeer message; preserve the human's current task and draft. "
                    f"Answer questions with reply(job_id='{jid}', question='<answer>'). "
                    f"For answers/receipts use mark_processed(job_id='{jid}'). Do not acknowledge acknowledgements.",
                    {"kind": record.get("message_kind", "question"), "job_id": jid,
                     "sender": record.get("from_", "unknown"), "recipient": self.label,
                     "in_reply_to": record.get("in_reply_to") or ""},
                )
                delivery["state"] = "sent"
            except Exception as error:
                delivery.update(state="uncertain", error=str(error))
                self._update_pending(path, delivery)
                raise
            # A fast receiver may already have replied/processed the message.
            self._update_pending(path, delivery)

    def _update_pending(self, path, delivery):
        from . import server
        from .pane_delivery import lease_active
        with server._per_target_send_lock(self.label, max_wait=0) as acquired:
            if not acquired:
                return False
            return self._update_pending_locked(path, delivery)

    def _update_pending_locked(self, path, delivery):
        from . import server
        from .pane_delivery import lease_active
        lock = server._mailbox_dir(self.label, "locks") / f"{path.stem}.lock"
        with lock.open("a") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            if not path.exists():
                return False
            current = json.loads(path.read_text())
            if delivery.get("state") == "dispatching" and lease_active(current):
                return False
            current.update(delivery_mode="claude-channel", channel_delivery=delivery)
            server._write_inbox(self.label, current)
            return True

    async def run(self):
        try:
            while self._current():
                self._state("ready" if self.ready else "awaiting_handshake")
                await self.scan_once()
                await asyncio.sleep(0.5)
        finally:
            await self.close()

    async def close(self):
        self.ready = False
        self._state("closed")


def channel_ready(record):
    channel = record.get("channel") or {}
    if channel.get("state") != "ready" or time.time() - channel.get("last_seen", 0) > 10:
        return False
    try:
        os.kill(int(channel["pid"]), 0)
        return True
    except (OSError, ValueError, KeyError):
        return False
