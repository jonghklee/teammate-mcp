"""FastMCP server exposing inter-agent Q&A tools.

Exposes:
    ask_codex(question, timeout)     — Claude → Codex
    ask_claude(question, timeout)    — Codex → Claude
    broadcast(message)               — fire-and-forget to both panes
    queue_status()                   — debugging snapshot

The MCP server is a long-lived stdio process spawned by Claude/Codex when
they start. Each tool call opens a short-lived iTerm Python API connection,
locates the target pane by `jobName`, pushes the question with a unique
marker, polls for the marker, then returns the extracted answer.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
import uuid
from typing import List, Optional

import iterm2
from mcp.server.fastmcp import FastMCP

from .iterm import (
    SessionRef,
    describe_panes,
    extract_answer,
    find_pane,
    find_session_by_job,
    get_screen,
    send_text,
    wait_for_marker,
)
from .log import get_logger
from .queue import MessageQueue
from . import registry


# Configurable through env so users can flip audit mode without code edits.
QUEUE_MODE = os.environ.get("TEAMMATE_QUEUE_MODE", "ephemeral")
PROJECT_CWD = os.environ.get("TEAMMATE_CWD") or os.getcwd()


def _jobname_for(agent: str) -> str:
    """Map agent label → expected jobName as iTerm reports it."""
    return {
        "claude": "claude",
        "codex": "codex",
    }.get(agent.lower(), agent.lower())


mcp = FastMCP("teammate")
_log = get_logger()
_queue = MessageQueue(mode=QUEUE_MODE)


async def _resolve_target(connection, spec: str, fallback_agent: Optional[str]) -> Optional[SessionRef]:
    """Resolve a target spec to an iTerm session.

    ``spec`` may be a label, session name, or session-id prefix (handled by
    ``find_pane``). When ``spec`` is empty *and* ``fallback_agent`` is set,
    fall back to the legacy 1:1 jobName lookup so existing
    ``ask_codex``/``ask_claude`` callers still work.
    """
    if spec:
        ref = await find_pane(connection, spec)
        if ref is not None:
            return ref
    if fallback_agent:
        return await find_session_by_job(
            connection, _jobname_for(fallback_agent), prefer_cwd=PROJECT_CWD
        )
    return None


async def _ask_async(
    question: str,
    timeout: int,
    target: str = "",
    fallback_agent: Optional[str] = None,
) -> str:
    """Drive one ask: enqueue → push → wait → extract → complete."""
    addressee = target or fallback_agent or "<unspecified>"
    from_agent = os.environ.get("TEAMMATE_LABEL") or fallback_agent or "unknown"
    msg = _queue.enqueue(from_agent, addressee, question, timeout=timeout)
    _log.event(
        "ask.enqueue",
        id=msg.id,
        from_=from_agent,
        to=addressee,
        target_spec=target or None,
        len=len(question),
    )

    connection = await iterm2.Connection.async_create()
    try:
        ref = await _resolve_target(connection, target, fallback_agent)
        if ref is None:
            _queue.fail(msg.id, "session not found")
            _log.event("ask.fail", id=msg.id, reason="session_not_found", target=addressee)
            if target:
                return f"ERROR: no iTerm pane matches target {target!r} (try mcp__teammate__list_panes)"
            return f"ERROR: no iTerm pane is currently running '{_jobname_for(fallback_agent or '')}'"

        marker = f"<<DONE_{msg.id}>>"
        body = (
            f"[teammate-mcp ASK {msg.id} from={from_agent}]\n"
            f"{question}\n\n"
            f"When you finish, output exactly this marker on its own line:\n"
            f"{marker}\n"
        )

        # Atomic claim before push so concurrent producers don't double-fire.
        _queue.claim(msg.id)
        await send_text(ref, body)
        _log.event("ask.send", id=msg.id, to=addressee, session_id=ref.session_id)

        # min_count=2: the prompt we just injected contains the marker
        # text, which gets echoed in the target pane. We must wait for a
        # *second* occurrence (= the actual reply) to avoid returning
        # immediately on our own injection.
        screen = await wait_for_marker(
            ref, marker, timeout=float(timeout), min_count=2
        )
        if screen is None:
            _queue.fail(msg.id, "timeout")
            _log.event("ask.timeout", id=msg.id, timeout=timeout)
            return f"TIMEOUT: no '{marker}' within {timeout}s"

        answer = extract_answer(screen, question, marker)
        _queue.complete(msg.id, answer)
        _log.event("ask.complete", id=msg.id, answer_len=len(answer))
        return answer or "(empty answer)"
    finally:
        try:
            connection.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# MCP tool surface
# ---------------------------------------------------------------------------

@mcp.tool()
async def ask(question: str, target: str = "", timeout: int = 300) -> str:
    """Ask another pane a question and return its answer.

    ``target`` may be:
      * a registered label (set via ``TEAMMATE_LABEL`` env or
        ``register_self``),
      * an iTerm session name (the title users edit with ``cmd+I``,
        case-insensitive exact match),
      * a session UUID prefix (≥ 6 chars).

    When ``target`` is empty the caller's job name is used: a Claude
    caller falls back to "codex" and vice versa, preserving the v0.1
    1:1 default behaviour.
    """
    fallback = "codex" if (os.environ.get("TEAMMATE_LABEL") or "").lower().startswith("claude") else None
    if not target and fallback is None:
        # We don't actually know which CLI is calling — let MCP decide
        # from the legacy aliases below.
        fallback = None
    return await _ask_async(question, timeout, target=target, fallback_agent=fallback)


@mcp.tool()
async def ask_codex(question: str, timeout: int = 300) -> str:
    """Legacy 1:1 helper. Prefer ``ask`` with an explicit ``target``."""
    return await _ask_async(question, timeout, target="", fallback_agent="codex")


@mcp.tool()
async def ask_claude(question: str, timeout: int = 300) -> str:
    """Legacy 1:1 helper. Prefer ``ask`` with an explicit ``target``."""
    return await _ask_async(question, timeout, target="", fallback_agent="claude")


@mcp.tool()
async def list_panes() -> list[dict]:
    """Return every live iTerm pane plus its label/name/id/job/cwd.

    Use this to see what targets are currently addressable. The shape of
    each entry is::

        {
          "label":        "worker"   | None,
          "session_name": "Worker A" | None,
          "session_id":   "B913A27E-…",
          "job":          "codex",
          "cwd":          "/path/…",
        }
    """
    connection = await iterm2.Connection.async_create()
    try:
        return await describe_panes(connection)
    finally:
        try:
            connection.close()
        except Exception:
            pass


@mcp.tool()
async def register_self(label: str) -> str:
    """Register the *calling* pane under ``label``.

    Looks up the calling process's iTerm session via its
    ``TERM_SESSION_ID`` env var. Subsequent ``ask(target=label, …)``
    calls will route to this pane.
    """
    tsid = os.environ.get("TERM_SESSION_ID", "")
    sid_tail = tsid.split(":", 1)[1] if ":" in tsid else tsid
    if not sid_tail:
        return "ERROR: no TERM_SESSION_ID — are you running inside iTerm?"

    connection = await iterm2.Connection.async_create()
    try:
        from .iterm import list_sessions
        refs = await list_sessions(connection)
        match = None
        for r in refs:
            if r.session_id.upper().endswith(sid_tail.upper()) or r.session_id.upper() == sid_tail.upper():
                match = r
                break
        if match is None:
            return f"ERROR: could not find iTerm session {sid_tail}"
        registry.register(
            label=label,
            session_id=match.session_id,
            pid=os.getpid(),
            job=match.job,
            cwd=match.cwd,
            extra={"session_name": match.name or None},
        )
        _log.event("register", label=label, session_id=match.session_id)
        return f"registered {match.session_id} as {label!r}"
    finally:
        try:
            connection.close()
        except Exception:
            pass


@mcp.tool()
async def unregister(label: str) -> str:
    """Remove a label from the registry."""
    registry.unregister(label)
    _log.event("unregister", label=label)
    return f"unregistered {label!r}"


@mcp.tool()
async def broadcast(message: str, targets: Optional[list[str]] = None) -> str:
    """Push a message to one or more panes without waiting for a reply.

    If ``targets`` is omitted, broadcasts to claude+codex (legacy mode).
    """
    connection = await iterm2.Connection.async_create()
    try:
        sent: list[str] = []
        if targets:
            for t in targets:
                ref = await find_pane(connection, t)
                if ref is not None:
                    await send_text(ref, f"[teammate-mcp BROADCAST] {message}")
                    sent.append(t)
        else:
            for agent in ("claude", "codex"):
                ref = await find_session_by_job(
                    connection, _jobname_for(agent), prefer_cwd=PROJECT_CWD
                )
                if ref is not None:
                    await send_text(ref, f"[teammate-mcp BROADCAST] {message}")
                    sent.append(agent)
        _log.event("broadcast", to=sent, len=len(message))
        return f"sent to: {', '.join(sent) if sent else 'nobody'}"
    finally:
        try:
            connection.close()
        except Exception:
            pass


@mcp.tool()
def queue_status() -> dict:
    """Return queue counts + recent completions (debugging)."""
    return _queue.status()


async def auto_register_session(connection, session_id: str,
                                 explicit_label: Optional[str] = None) -> Optional[dict]:
    """Register an iTerm pane (by id) into the global registry.

    Used by spawn helpers (`bin/team`, demo scripts) so a pane is
    addressable *immediately* after launch — before its CLI gets a chance
    to invoke any MCP tool. Returns the registered record or None on miss.

    If the pane is already registered (same session_id), reuses the
    existing label instead of inventing a new one — prevents the
    ``agent1`` and ``agent2`` both pointing at the same pane.
    """
    from .iterm import list_sessions
    refs = await list_sessions(connection)
    sid_up = session_id.upper()
    me = next((r for r in refs if r.session_id.upper() == sid_up), None)
    if me is None:
        return None

    # Reuse existing label if this pane is already registered.
    existing_label: Optional[str] = None
    for label, rec in registry.all_labels().items():
        if (rec.get("session_id") or "").upper() == sid_up:
            existing_label = label
            break

    label = explicit_label or existing_label or _next_auto_label(me.job, me.name)
    registry.register(
        label=label,
        session_id=me.session_id,
        pid=os.getpid(),
        job=me.job,
        cwd=me.cwd,
        extra={"session_name": me.name or None,
               "auto_assigned": not explicit_label},
    )
    return {
        "label": label,
        "session_id": me.session_id,
        "job": me.job,
        "cwd": me.cwd,
    }


def _classify(job: str, session_name: str = "") -> str:
    """Decide the label prefix from job + session_name hints.

    Claude Code reports its jobName as 'Python' or 'claude.exe' depending
    on platform, but its session_name typically contains 'Claude Code'.
    Codex is more honest and reports 'codex'. We check both fields.
    """
    haystack = f"{job or ''} {session_name or ''}".lower()
    if "claude" in haystack:
        return "claude"
    if "codex" in haystack:
        return "codex"
    return "agent"


def _next_auto_label(job: str, session_name: str = "") -> str:
    """Pick the next free ``{base}{n}`` label."""
    base = _classify(job, session_name)
    used = set(registry.all_labels().keys())
    n = 1
    while f"{base}{n}" in used:
        n += 1
    return f"{base}{n}"


def _auto_register_from_env() -> None:
    """Attach a label to the calling pane on server startup.

    Order:
      1. If ``TEAMMATE_LABEL`` is exported, use it verbatim.
      2. Otherwise auto-assign the next free ``{job}{n}`` slot
         (``claude1``, ``codex1``, ``codex2`` …).

    Non-fatal: if iTerm's Python API isn't reachable we silently skip.
    """
    explicit = os.environ.get("TEAMMATE_LABEL", "").strip()
    tsid = os.environ.get("TERM_SESSION_ID", "")
    sid_tail = tsid.split(":", 1)[1] if ":" in tsid else tsid
    if not sid_tail:
        return

    async def _go():
        try:
            connection = await iterm2.Connection.async_create()
        except Exception:
            return
        try:
            from .iterm import list_sessions
            refs = await list_sessions(connection)
            me = None
            for r in refs:
                if r.session_id.upper().endswith(sid_tail.upper()):
                    me = r
                    break
            if me is None:
                return
            # Reuse an existing label for this pane if there is one.
            existing_label = next(
                (l for l, r in registry.all_labels().items()
                 if (r.get("session_id") or "").upper() == me.session_id.upper()),
                None,
            )
            label = explicit or existing_label or _next_auto_label(me.job, me.name or "")
            registry.register(
                label=label,
                session_id=me.session_id,
                pid=os.getpid(),
                job=me.job,
                cwd=me.cwd,
                extra={"session_name": me.name or None,
                       "auto_assigned": not explicit},
            )
            # Make the chosen label visible to the *current* server
            # process — used as `from_agent` in queue records.
            os.environ["TEAMMATE_LABEL"] = label
            _log.event("auto_register", label=label,
                       session_id=me.session_id, auto=not explicit)
        finally:
            try:
                connection.close()
            except Exception:
                pass

    try:
        asyncio.get_event_loop().run_until_complete(_go())
    except RuntimeError:
        asyncio.run(_go())
    except Exception:
        pass


def main():
    """Entry point used by `teammate-mcp` console script."""
    _log.event("server.start", queue_mode=QUEUE_MODE, cwd=PROJECT_CWD)
    _auto_register_from_env()
    mcp.run()


if __name__ == "__main__":
    main()
