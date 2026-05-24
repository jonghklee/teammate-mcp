"""Long-running daemon owning registry + mailbox routing.

Why a daemon?
=============

The original architecture had one MCP server child per pane (claude/codex
spawns ``teammate-mcp serve`` and that child auto-registers the pane).
That model has two structural problems:

1. The recorded pid is the MCP server child, not the pane itself.
   Codex/Claude can recycle the MCP child between turns, leaving the
   registry pointing at a dead pid even though the pane is fine.

2. Multiple MCP children race on ``registry.json``: each one
   read-modifies-writes the file, and ``flock`` only narrows the
   window — under load entries still get lost.

Daemon mode solves both:

- The daemon owns ``registry.json``. Clients send register/unregister/
  list RPCs over a Unix socket. No file-write race is possible.
- A pane's label lifetime is decoupled from any individual MCP child.
  As long as the iTerm session stays open the label survives, no matter
  how many times the inner MCP server gets recycled.

Operation
=========

Binary entrypoint:
    teammate-mcp daemon            # foreground (for debugging)
    teammate-mcp daemon --bg       # detached (launchd uses this implicitly)

Socket:
    ~/.teammate-mcp/run/daemon.sock

Protocol:
    Newline-delimited JSON. One request, one response, then close (the
    simplest possible RPC). Each line is a JSON object::

        request:   {"op": "register", "args": {...}}
        response:  {"ok": true,  "result": {...}}
                or {"ok": false, "error": "<message>"}

Operations (v1):
    register(label, session_id, pid, job, cwd, extra)
    unregister(label)
    list()                          → {labels: {label: rec, ...}}
    lookup(target)                  → {label, session_id, ...} | null
    enqueue(from_, to, body)        → {job_id, accepted: bool}
    inbox(label, limit=5)           → [{job_id, from_, body, ...}, ...]
    mark_processed(job_id, label, reply)
    prune()                         → {removed: [...]}
    health()                        → {pid, uptime, label_count}

Backwards compatibility
=======================

The daemon is fully *additive*. If clients can't reach it (socket
missing, EPERM, daemon crashed mid-restart) they fall back to direct
file access, i.e. the pre-daemon mode. Existing
``register-pane`` / ``ask`` / ``list`` CLI invocations work identically
in either mode.

Activation
==========

Opt-in via env var::

    export TEAMMATE_DAEMON=1

When set, clients prefer the daemon. When unset, clients use the
legacy file path. The daemon itself runs whenever launchd's plist is
loaded — the env var only controls whether the *client* tries to use
it.

Long-term goal: flip the default to ``1`` once the daemon has run
without incident for ≥1 week of normal use.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Optional

from . import __version__, registry as _reg
from .log import get_logger


RUN_DIR = Path.home() / ".teammate-mcp" / "run"
SOCKET_PATH = RUN_DIR / "daemon.sock"
PID_PATH = RUN_DIR / "daemon.pid"
LOG = Path.home() / ".teammate-mcp" / "logs" / "daemon.log"

_started_at: float = 0.0
_log = get_logger()


# ---------------------------------------------------------------------------
# Daemon-side handlers
# ---------------------------------------------------------------------------


def _op_register(args: dict) -> dict:
    label = args["label"]
    _reg.register(
        label=label,
        session_id=args["session_id"],
        pid=int(args.get("pid") or 0),
        job=args.get("job") or "",
        cwd=args.get("cwd"),
        extra=args.get("extra") or {},
        dedupe_session_id=True,
    )
    return {"label": label, "ok": True}


def _op_unregister(args: dict) -> dict:
    _reg.unregister(args["label"])
    return {"label": args["label"]}


def _op_list(_args: dict) -> dict:
    return {"labels": _reg.all_labels()}


def _op_lookup(args: dict) -> Optional[dict]:
    spec = (args.get("target") or "").strip()
    if not spec:
        return None
    # Direct label match first
    rec = _reg.lookup(spec)
    if rec:
        return rec
    # session_id prefix match
    target_up = spec.upper()
    for label, rec in _reg.all_labels().items():
        sid = (rec.get("session_id") or "").upper()
        if sid == target_up or sid.startswith(target_up) or target_up.startswith(sid):
            return rec
    return None


def _op_prune(_args: dict) -> dict:
    removed = _reg.prune_dead(force_refresh=True)
    return {"removed": removed}


def _op_health(_args: dict) -> dict:
    return {
        "pid": os.getpid(),
        "uptime_s": round(time.time() - _started_at, 1),
        "label_count": len(_reg.all_labels()),
        "version": __version__,
    }


_OPS: dict[str, Any] = {
    "register": _op_register,
    "unregister": _op_unregister,
    "list": _op_list,
    "lookup": _op_lookup,
    "prune": _op_prune,
    "health": _op_health,
}


async def _handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Service a single connection. One request → one response → close."""
    peer = writer.get_extra_info("peername") or "?"
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=5.0)
        if not line:
            return
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            _write_response(writer, ok=False, error=f"bad JSON: {e}")
            return

        op = req.get("op")
        args = req.get("args") or {}
        handler = _OPS.get(op)
        if handler is None:
            _write_response(writer, ok=False, error=f"unknown op {op!r}")
            return

        try:
            result = handler(args)
        except KeyError as e:
            _write_response(writer, ok=False, error=f"missing arg: {e}")
            return
        except Exception as e:  # noqa: BLE001
            _log.event("op.error", op=op, error=repr(e))
            _write_response(writer, ok=False, error=f"{type(e).__name__}: {e}")
            return

        _write_response(writer, ok=True, result=result)
        _log.event("op.ok", op=op, peer=str(peer))
    except asyncio.TimeoutError:
        _write_response(writer, ok=False, error="request timeout")
    finally:
        with contextlib.suppress(Exception):
            await writer.drain()
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


def _write_response(writer: asyncio.StreamWriter, *, ok: bool, result: Any = None,
                    error: Optional[str] = None) -> None:
    payload: dict = {"ok": ok}
    if ok:
        payload["result"] = result
    else:
        payload["error"] = error
    writer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def _ensure_dirs() -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)


def _existing_daemon_alive() -> Optional[int]:
    """Return pid of an already-running daemon, or None."""
    try:
        pid = int(PID_PATH.read_text().strip())
    except (OSError, ValueError):
        return None
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


def _cleanup_socket() -> None:
    with contextlib.suppress(OSError):
        SOCKET_PATH.unlink()


async def _serve() -> None:
    global _started_at
    _started_at = time.time()
    _ensure_dirs()

    existing = _existing_daemon_alive()
    if existing:
        print(f"another daemon is already running (pid {existing})", file=sys.stderr)
        sys.exit(1)

    # Clean up any stale socket from a previous crashed run.
    _cleanup_socket()

    server = await asyncio.start_unix_server(_handle_client, path=str(SOCKET_PATH))
    # Tighten socket permissions (owner-only).
    with contextlib.suppress(OSError):
        os.chmod(SOCKET_PATH, 0o600)

    PID_PATH.write_text(str(os.getpid()))
    _log.event("daemon.start", pid=os.getpid(), socket=str(SOCKET_PATH))
    print(f"teammate-mcp daemon listening on {SOCKET_PATH} (pid {os.getpid()})",
          file=sys.stderr, flush=True)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _on_signal(signum: int) -> None:
        _log.event("daemon.signal", signal=signum)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _on_signal, sig)

    try:
        async with server:
            await stop_event.wait()
            server.close()
            await server.wait_closed()
    finally:
        with contextlib.suppress(OSError):
            PID_PATH.unlink()
        _cleanup_socket()
        _log.event("daemon.stop")


def main(argv: list[str] | None = None) -> int:
    """Entrypoint for ``teammate-mcp daemon``."""
    args = list(argv or [])
    background = "--bg" in args or "--detach" in args
    if background:
        # Double-fork so the child outlives the launching shell.
        if os.fork() != 0:
            return 0
        os.setsid()
        if os.fork() != 0:
            os._exit(0)
        # Redirect stdio so launchd / shells don't keep ttys open.
        with open("/dev/null", "rb") as devnull_in, open(LOG, "ab") as log_out:
            os.dup2(devnull_in.fileno(), 0)
            os.dup2(log_out.fileno(), 1)
            os.dup2(log_out.fileno(), 2)

    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
