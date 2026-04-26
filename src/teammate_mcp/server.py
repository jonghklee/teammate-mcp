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
from typing import Optional

import iterm2
from mcp.server.fastmcp import FastMCP

from .iterm import (
    SessionRef,
    extract_answer,
    find_session_by_job,
    get_screen,
    send_text,
    wait_for_marker,
)
from .log import get_logger
from .queue import MessageQueue


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


async def _ask_async(target_agent: str, question: str, timeout: int) -> str:
    """Drive one ask: enqueue → push → wait → extract → complete."""
    from_agent = "claude" if target_agent == "codex" else "codex"
    msg = _queue.enqueue(from_agent, target_agent, question, timeout=timeout)
    _log.event("ask.enqueue", id=msg.id, from_=from_agent, to=target_agent, len=len(question))

    connection = await iterm2.Connection.async_create()
    try:
        target = await find_session_by_job(
            connection,
            _jobname_for(target_agent),
            prefer_cwd=PROJECT_CWD,
        )
        if target is None:
            _queue.fail(msg.id, "session not found")
            _log.event("ask.fail", id=msg.id, reason="session_not_found")
            return f"ERROR: no iTerm pane is currently running '{_jobname_for(target_agent)}'"

        marker = f"<<DONE_{msg.id}>>"
        body = (
            f"[teammate-mcp ASK {msg.id} from={from_agent}]\n"
            f"{question}\n\n"
            f"When you finish, output exactly this marker on its own line:\n"
            f"{marker}\n"
        )

        # Atomic claim before push so concurrent producers don't double-fire.
        _queue.claim(msg.id)
        await send_text(target, body)
        _log.event("ask.send", id=msg.id, to=target_agent, session_id=target.session_id)

        # min_count=2: the prompt we just injected contains the marker
        # text, which gets echoed in the target pane. We must wait for a
        # *second* occurrence (= the actual reply) to avoid returning
        # immediately on our own injection.
        screen = await wait_for_marker(
            target, marker, timeout=float(timeout), min_count=2
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
async def ask_codex(question: str, timeout: int = 300) -> str:
    """Ask the Codex pane a question and return its answer.

    Use this from Claude when you want Codex's opinion, a code review,
    or to delegate execution to it.
    """
    return await _ask_async("codex", question, timeout)


@mcp.tool()
async def ask_claude(question: str, timeout: int = 300) -> str:
    """Ask the Claude pane a question and return its answer.

    Use this from Codex when you want Claude's plan, design feedback,
    or a sanity check.
    """
    return await _ask_async("claude", question, timeout)


@mcp.tool()
async def broadcast(message: str) -> str:
    """Push a message to both panes without waiting for a reply."""
    connection = await iterm2.Connection.async_create()
    try:
        sent = []
        for agent in ("claude", "codex"):
            ref = await find_session_by_job(
                connection, _jobname_for(agent), prefer_cwd=PROJECT_CWD
            )
            if ref is not None:
                await send_text(ref, f"[teammate-mcp BROADCAST] {message}")
                sent.append(agent)
        _log.event("broadcast", to=sent, len=len(message))
        return f"sent to: {', '.join(sent) if sent else 'nobody (no matching panes)'}"
    finally:
        try:
            connection.close()
        except Exception:
            pass


@mcp.tool()
def queue_status() -> dict:
    """Return queue counts + recent completions (debugging)."""
    return _queue.status()


def main():
    """Entry point used by `teammate-mcp` console script."""
    _log.event("server.start", queue_mode=QUEUE_MODE, cwd=PROJECT_CWD)
    mcp.run()


if __name__ == "__main__":
    main()
