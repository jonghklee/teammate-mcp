"""Thin client for the daemon Unix socket.

All operations have a synchronous wrapper that opens one connection,
sends one request, reads one response, and closes. If the daemon is
unreachable (socket missing, EPERM, refused) every operation returns
``None`` so the caller can fall back to the legacy file-based path.

The daemon is OFF by default. ``is_enabled()`` returns True only when
``TEAMMATE_DAEMON`` is set to a truthy value AND the socket exists.
This lets us roll the daemon out incrementally without breaking
anyone who hasn't opted in.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any, Optional


SOCKET_PATH = Path.home() / ".teammate-mcp" / "run" / "daemon.sock"
_TIMEOUT = 5.0


def is_enabled() -> bool:
    """True iff the user opted into daemon mode AND the daemon is up.

    Opt-in: ``TEAMMATE_DAEMON`` env var set to ``1``, ``true``, ``yes``,
    or ``on`` (case-insensitive). If not set, the daemon is never
    consulted even if it's running — preserves legacy behavior.
    """
    val = (os.environ.get("TEAMMATE_DAEMON") or "").strip().lower()
    if val not in ("1", "true", "yes", "on"):
        return False
    return SOCKET_PATH.exists()


def _call(op: str, args: Optional[dict] = None,
          timeout: float = _TIMEOUT) -> Optional[dict]:
    """Send one request, return its ``result`` field, or None on any error.

    Errors swallowed silently so callers can fall back transparently.
    Use ``raw_call`` if you need to distinguish failure modes.
    """
    res = raw_call(op, args, timeout=timeout)
    if res is None:
        return None
    if not res.get("ok"):
        return None
    return res.get("result") or {}


def raw_call(op: str, args: Optional[dict] = None,
             timeout: float = _TIMEOUT) -> Optional[dict]:
    """Like ``_call`` but returns the full response envelope (including
    ``ok`` flag and ``error`` message). Returns None only when the
    daemon is unreachable.
    """
    payload = {"op": op, "args": args or {}}
    line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(SOCKET_PATH))
            s.sendall(line)
            # Read one response line.
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > 4 * 1024 * 1024:
                    return None  # absurd response, drop
        if not buf:
            return None
        try:
            return json.loads(buf.decode("utf-8").splitlines()[0])
        except (UnicodeDecodeError, json.JSONDecodeError, IndexError):
            return None
    except (FileNotFoundError, ConnectionRefusedError, PermissionError,
            socket.timeout, OSError):
        return None


# ---------------------------------------------------------------------------
# Typed convenience wrappers
# ---------------------------------------------------------------------------


def register(label: str, session_id: str, pid: int, job: str,
             cwd: Optional[str] = None,
             extra: Optional[dict] = None) -> Optional[dict]:
    return _call("register", {
        "label": label,
        "session_id": session_id,
        "pid": pid,
        "job": job,
        "cwd": cwd,
        "extra": extra or {},
    })


def unregister(label: str) -> Optional[dict]:
    return _call("unregister", {"label": label})


def list_labels() -> Optional[dict]:
    return _call("list", {})


def lookup(target: str) -> Optional[dict]:
    return _call("lookup", {"target": target})


def prune() -> Optional[dict]:
    return _call("prune", {})


def health() -> Optional[dict]:
    return _call("health", {})
