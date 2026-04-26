"""Live-iTerm sanity tests with structured timing output.

Single combined scenario keeps the iterm2-library websocket lifecycle
deterministic (running multiple separate connections in series within a
single pytest process is racey across versions).

For full bidirectional `ask_codex` / `ask_claude` validation against real
Claude Code + Codex CLIs, see ``tests/manual_demo.md`` — that flow needs
two interactive panes and two API providers, so it is intentionally not
part of the automated suite.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest


pytestmark = pytest.mark.asyncio


def _iterm_running() -> bool:
    try:
        for arg in ("iTerm2", "iTerm"):
            out = subprocess.run(
                ["pgrep", "-i", "-x", arg],
                capture_output=True,
                text=True,
                check=False,
            )
            if out.returncode == 0 and out.stdout.strip():
                return True
        ps = subprocess.run(
            ["ps", "-axco", "command"], capture_output=True, text=True, check=False
        )
        return any("iTerm" in line for line in ps.stdout.splitlines())
    except FileNotFoundError:
        return False


RESULTS_DIR = Path(__file__).parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)


@pytest.mark.skipif(not _iterm_running(), reason="iTerm2 not running")
async def test_live_iterm_handshake_and_lookup():
    """Single combined live test:

    1. Open iTerm Python API connection.
    2. Enumerate all sessions and capture jobName diversity.
    3. If a `claude` / `codex` pane is currently running anywhere, prove
       ``find_session_by_job`` locates it (this is the same lookup the
       MCP server uses on every ask call).

    Records timing to ``tests/results/<timestamp>-live.jsonl``.
    """
    import iterm2
    from teammate_mcp.iterm import find_session_by_job, list_sessions

    timings: list[tuple[str, int]] = []
    findings: dict = {}

    t0 = time.monotonic()
    connection = await iterm2.Connection.async_create()
    timings.append(("connect", int((time.monotonic() - t0) * 1000)))
    try:
        # Phase A — enumerate.
        t = time.monotonic()
        refs = await list_sessions(connection)
        timings.append(("list_sessions", int((time.monotonic() - t) * 1000)))
        assert len(refs) >= 1
        assert all(r.session_id for r in refs)
        findings["session_count"] = len(refs)
        findings["distinct_jobs"] = sorted({r.job for r in refs if r.job})

        # Phase B — locate each known CLI if present.
        for needle in ("claude", "codex"):
            present = any(
                r.job.lower() == needle or needle in r.command_line.lower()
                for r in refs
            )
            if not present:
                findings.setdefault("not_running", []).append(needle)
                continue
            t = time.monotonic()
            found = await find_session_by_job(connection, needle)
            timings.append((f"find_{needle}", int((time.monotonic() - t) * 1000)))
            assert found is not None, (
                f"{needle!r} appears in iTerm but find_session_by_job returned None"
            )
            findings.setdefault("located", {})[needle] = found.session_id
    finally:
        try:
            connection.close()
        except Exception:
            pass

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    record = {
        "scenario": "live_handshake_and_lookup",
        "timestamp": stamp,
        "phases": [{"name": n, "elapsed_ms": v} for n, v in timings],
        "total_ms": int((time.monotonic() - t0) * 1000),
        **findings,
    }
    out_path = RESULTS_DIR / f"{stamp}-live.jsonl"
    out_path.write_text(json.dumps(record, ensure_ascii=False, indent=2))
    print("\n=== timing report ===")
    print(json.dumps(record, ensure_ascii=False, indent=2))
