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
    osa_capture,
    osa_send_text,
    osa_session_alive,
    osa_wait_for_marker,
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
    """Resolve a target spec to an iTerm session — REGISTRY ONLY.

    Behavior change in v0.3.0: we no longer fall back to scanning the
    process table for any matching jobName. Only panes the user has
    explicitly registered (via ``/team-register`` or ``register_self``)
    are addressable. This matches the user's mental model: "only tagged
    panes participate".

    For ``spec``: try ``find_pane`` (label / session name / id prefix).
    For ``fallback_agent``: look up the registry for any registered pane
    whose recorded ``job`` matches the agent name. Returns None if no
    registered pane matches.
    """
    if spec:
        return await find_pane(connection, spec)

    if fallback_agent:
        wanted = fallback_agent.lower()
        from .iterm import list_sessions
        live_sids = {r.session_id.upper() for r in await list_sessions(connection)}
        for label, rec in registry.all_labels().items():
            if (rec.get("job") or "").lower() != wanted:
                continue
            sid = (rec.get("session_id") or "").upper()
            if sid in live_sids:
                return await find_pane(connection, label)
    return None


def _resolve_target_session_id(target: str, fallback_agent: Optional[str]) -> Optional[str]:
    """Pure-registry, pure-Python target → session_id resolver.

    No iterm2 lib, no AppleScript. Looks up the label in the registry
    and returns its recorded session_id. For the 1:1 fallback, scans
    the registry for any entry whose recorded ``job`` matches the
    requested agent name.

    Used by the new osascript-only ask path; fast and immune to the
    iterm2 lib's per-pane variable-query stalls.
    """
    if target:
        rec = registry.lookup(target)
        if rec:
            sid = (rec.get("session_id") or "").strip()
            if sid:
                return sid
        return None
    if fallback_agent:
        wanted = fallback_agent.lower()
        for rec in registry.all_labels().values():
            if (rec.get("job") or "").lower() == wanted:
                sid = (rec.get("session_id") or "").strip()
                if sid:
                    return sid
    return None


async def _ask_async(
    question: str,
    timeout: int,
    target: str = "",
    fallback_agent: Optional[str] = None,
) -> str:
    """Drive one ask: enqueue → push (osascript) → wait (osascript) → return.

    Bypasses the iterm2 Python library entirely on the hot path because
    `async_get_app(connection)` and per-session variable queries can
    hang on desktops with many panes. AppleScript via `osascript`
    addresses the target session by id directly and is unaffected.
    """
    addressee = target or fallback_agent or "<unspecified>"
    from_agent = os.environ.get("TEAMMATE_LABEL") or fallback_agent or "unknown"
    msg = _queue.enqueue(from_agent, addressee, question, timeout=timeout)
    _log.event(
        "ask.enqueue",
        id=msg.id, from_=from_agent, to=addressee,
        target_spec=target or None, len=len(question),
    )

    sid = _resolve_target_session_id(target, fallback_agent)
    _log.event("ask.resolve", id=msg.id, found=sid is not None, session_id=sid)
    if sid is None:
        _queue.fail(msg.id, "session not found")
        _log.event("ask.fail", id=msg.id, reason="not_in_registry", target=addressee)
        if target:
            return (
                f"ERROR: no registered pane matches target {target!r}.\n"
                f"Hint: in the target pane, run `teammate-mcp register-pane` "
                f"(or use the tmclaude/tmcodex wrappers)."
            )
        return (
            f"ERROR: no registered '{fallback_agent}' pane.\n"
            f"Hint: in the {fallback_agent} pane, run `teammate-mcp register-pane`."
        )

    if not osa_session_alive(sid):
        registry.unregister(addressee if target else "")
        _queue.fail(msg.id, "session vanished")
        _log.event("ask.fail", id=msg.id, reason="session_dead", session_id=sid)
        return (
            f"ERROR: registered pane {addressee} (session {sid[:8]}…) is no "
            f"longer open in iTerm. Re-register the new pane."
        )

    marker = f"<<DONE_{msg.id}>>"
    body = (
        f"[teammate-mcp ASK {msg.id} from={from_agent}]\n"
        f"{question}\n\n"
        f"When you finish, output exactly this marker on its own line:\n"
        f"{marker}\n"
    )

    _queue.claim(msg.id)
    _log.event("ask.send_start", id=msg.id, to=addressee, session_id=sid)
    try:
        # Run the (sync) subprocess send in a thread so we don't block
        # the asyncio loop.
        await asyncio.to_thread(osa_send_text, sid, body, True)
    except Exception as e:
        _queue.fail(msg.id, f"send_text failed: {e!r}")
        _log.event("ask.fail", id=msg.id, reason="send_failed", error=repr(e))
        return f"ERROR: send_text via osascript failed: {e}"
    _log.event("ask.send", id=msg.id, to=addressee, session_id=sid)

    # min_count=2: the prompt we injected contains the marker; wait for
    # the SECOND occurrence (the agent's actual reply terminator).
    screen = await osa_wait_for_marker(
        sid, marker, timeout=float(timeout), poll_interval=2.0, min_count=2,
    )
    if screen is None:
        _queue.fail(msg.id, "timeout")
        _log.event("ask.timeout", id=msg.id, timeout=timeout)
        return f"TIMEOUT: no '{marker}' within {timeout}s"

    answer = extract_answer(screen, question, marker)
    _queue.complete(msg.id, answer)
    _log.event("ask.complete", id=msg.id, answer_len=len(answer))
    return answer or "(empty answer)"


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
async def register_self(label: str = "") -> str:
    """Register the *calling* pane.

    If ``label`` is empty (the default), an auto label is assigned:
    ``claude1`` / ``codex1`` / ``codex2`` / etc., based on the pane's
    job and the next free slot. Subsequent ``ask(target=label, …)``
    calls route to this pane.

    The returned string includes the chosen label so the caller can
    print it back to the user — they should *also* set their iTerm
    tab title to that label so it's visible at the bottom of the pane.
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

        # Reuse existing label if this pane is already registered.
        existing_label = next(
            (l for l, r in registry.all_labels().items()
             if (r.get("session_id") or "").upper() == match.session_id.upper()),
            None,
        )
        chosen = label.strip() or existing_label or _next_auto_label(match.job, match.name or "")

        registry.register(
            label=chosen,
            session_id=match.session_id,
            pid=os.getpid(),
            job=match.job,
            cwd=match.cwd,
            extra={"session_name": match.name or None,
                   "auto_assigned": not label.strip()},
        )

        # Try to set the iTerm tab title so the label is visible to the
        # user without requiring `bin/install-statusline`.
        try:
            await match.session.async_send_text(
                f"\x1b]2;[{chosen}]\x07"
            )
        except Exception:
            pass

        _log.event("register_self", label=chosen, session_id=match.session_id,
                   auto=not label.strip())
        return f"registered as '{chosen}' (session {match.session_id[:8]}…)"
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
