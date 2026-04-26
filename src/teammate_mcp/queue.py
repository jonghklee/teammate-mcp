"""File-system backed FIFO queue for inter-agent messages.

Two storage modes:
- ``ephemeral`` (default): a temp directory, cleaned up at process exit.
- ``audit``: a ``.queue/`` folder under the project cwd; survives across runs
  and can be committed to git.

Lock semantics rely on POSIX ``rename`` atomicity: moving a file from
``pending/`` to ``inflight/`` is an atomic claim.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional


@dataclass
class Message:
    id: str
    from_agent: str
    to_agent: str
    question: str
    created_at: float
    priority: int = 1
    timeout: int = 300
    answer: Optional[str] = None
    completed_at: Optional[float] = None
    failed_reason: Optional[str] = None


class MessageQueue:
    def __init__(self, mode: str = "ephemeral", base: Optional[Path] = None):
        if mode not in ("ephemeral", "audit"):
            raise ValueError(f"unknown mode: {mode!r}")
        self.mode = mode
        if base is None:
            if mode == "audit":
                base = Path.cwd() / ".queue"
            else:
                base = Path(tempfile.mkdtemp(prefix="teammate-mcp-"))
                # auto-clean ephemeral dirs unless caller provides their own.
                atexit.register(shutil.rmtree, base, ignore_errors=True)
        self.base = Path(base)
        for sub in ("pending", "inflight", "done", "failed"):
            (self.base / sub).mkdir(parents=True, exist_ok=True)

    # ---------- producer side ----------
    def enqueue(
        self,
        from_agent: str,
        to_agent: str,
        question: str,
        priority: int = 1,
        timeout: int = 300,
    ) -> Message:
        ts = time.time()
        # millisecond timestamp prefix to maintain FIFO when multiple
        # messages enqueued in the same second.
        msg_id = f"{int(ts*1000):013d}-{uuid.uuid4().hex[:6]}"
        msg = Message(
            id=msg_id,
            from_agent=from_agent,
            to_agent=to_agent,
            question=question,
            created_at=ts,
            priority=priority,
            timeout=timeout,
        )
        self._write(self.base / "pending" / f"{msg_id}.json", msg)
        return msg

    # ---------- consumer side ----------
    def claim(self, msg_id: str) -> Optional[Message]:
        """Move a message from pending → inflight atomically.

        Returns the message on success, ``None`` if another worker beat us.
        """
        src = self.base / "pending" / f"{msg_id}.json"
        dst = self.base / "inflight" / f"{msg_id}.json"
        try:
            os.rename(src, dst)
        except FileNotFoundError:
            return None
        except OSError:
            return None
        return self._read(dst)

    def complete(self, msg_id: str, answer: str) -> None:
        src = self.base / "inflight" / f"{msg_id}.json"
        dst = self.base / "done" / f"{msg_id}.json"
        msg = self._read(src)
        if msg is None:
            return
        msg.answer = answer
        msg.completed_at = time.time()
        self._write(dst, msg)
        # Persist the answer alongside (separate file simplifies tailing).
        (self.base / "done" / f"{msg_id}.answer.txt").write_text(answer)
        try:
            os.remove(src)
        except FileNotFoundError:
            pass

    def fail(self, msg_id: str, reason: str) -> None:
        src = self.base / "inflight" / f"{msg_id}.json"
        if not src.exists():
            src = self.base / "pending" / f"{msg_id}.json"
        dst = self.base / "failed" / f"{msg_id}.json"
        msg = self._read(src)
        if msg is None:
            return
        msg.failed_reason = reason
        msg.completed_at = time.time()
        self._write(dst, msg)
        try:
            os.remove(src)
        except FileNotFoundError:
            pass

    # ---------- introspection ----------
    def status(self) -> dict:
        def _count(sub: str) -> int:
            return len(list((self.base / sub).glob("*.json")))

        recent_done = sorted(
            (self.base / "done").glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:5]
        return {
            "mode": self.mode,
            "base": str(self.base),
            "pending": _count("pending"),
            "inflight": _count("inflight"),
            "done": _count("done"),
            "failed": _count("failed"),
            "recent_done_ids": [p.stem for p in recent_done],
        }

    # ---------- internals ----------
    @staticmethod
    def _write(path: Path, msg: Message) -> None:
        # Serialise with `from_agent` / `to_agent` keys (avoid Python
        # `from` reserved word collision in JSON consumers).
        data = asdict(msg)
        path.write_text(json.dumps(data, indent=2))

    @staticmethod
    def _read(path: Path) -> Optional[Message]:
        try:
            data = json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        return Message(**data)
