"""Automated bidirectional demo.

1. Spawn a fresh iTerm window split into [claude | codex] panes.
2. Wait for both CLIs to boot.
3. Inject a prompt into the Claude pane that *requires* it to call
   ``mcp__teammate__ask_codex`` and end its reply with a sentinel marker.
4. Poll the Claude pane until the marker appears, then dump:
   - the answer Claude received from Codex,
   - per-phase timings,
   - location of the structured log.

Usage:
    python scripts/auto_demo.py

Requires:
    - claude / codex CLIs logged in
    - teammate-mcp registered with both (`claude mcp add`, `codex mcp add`)
    - iTerm Python API enabled
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import iterm2

# Project import
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from teammate_mcp.iterm import (  # noqa: E402
    clear_input,
    extract_answer,
    find_session_by_job,
    get_screen,
    list_sessions,
    send_text,
    wait_for_marker,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = PROJECT_ROOT / "tests" / "results"
RESULTS_DIR.mkdir(exist_ok=True)


def spawn_window() -> tuple[str, str]:
    """Open a fresh iTerm window with claude (left) and codex (right).

    Returns ``(claude_session_id, codex_session_id)`` taken directly from
    the new sessions so we never confuse them with pre-existing panes.

    Strategy: create the empty pane first, capture its ``unique id``, then
    *export* both ids as environment variables before launching the CLIs.
    The CLIs spawn the teammate-mcp server, which inherits those env
    vars and uses them to pin the correct sibling pane (no fuzzy lookup).
    """
    cwd = str(PROJECT_ROOT)

    # Step 1: create empty panes and harvest their ids.
    osa_step1 = '''
tell application "iTerm"
    activate
    set newWindow to (create window with default profile)
    set leftSession to current session of newWindow
    tell leftSession
        set rightSession to (split vertically with same profile)
    end tell
    return (unique id of leftSession) & "|" & (unique id of rightSession)
end tell
'''
    out = subprocess.check_output(["osascript", "-e", osa_step1], text=True).strip()
    parts = out.split("|")
    if len(parts) != 2 or not all(parts):
        raise RuntimeError(f"unexpected osascript output: {out!r}")
    left_id, right_id = parts

    # Record ids in the spawn ledger so cleanup_my_panes.py can find them
    # later — and so cleanup never confuses them with sibling user panes.
    from teammate_mcp import spawn_track
    spawn_track.record(left_id, spawned_by="scripts/auto_demo.py")
    spawn_track.record(right_id, spawned_by="scripts/auto_demo.py")

    # Step 2: now that we know the ids, ask AppleScript to type the
    # `cd + export + launch` sequence into each pane.
    osa_step2 = f'''
tell application "iTerm"
    repeat with w in windows
        repeat with t in tabs of w
            repeat with s in sessions of t
                if (unique id of s) is "{left_id}" then
                    tell s
                        write text "cd {cwd}"
                        write text "export TEAMMATE_CLAUDE_SESSION_ID={left_id}"
                        write text "export TEAMMATE_CODEX_SESSION_ID={right_id}"
                        write text "export TEAMMATE_CWD={cwd}"
                        write text "claude --dangerously-skip-permissions"
                    end tell
                else if (unique id of s) is "{right_id}" then
                    tell s
                        write text "cd {cwd}"
                        write text "export TEAMMATE_CLAUDE_SESSION_ID={left_id}"
                        write text "export TEAMMATE_CODEX_SESSION_ID={right_id}"
                        write text "export TEAMMATE_CWD={cwd}"
                        write text "codex --yolo"
                    end tell
                end if
            end repeat
        end repeat
    end repeat
end tell
'''
    subprocess.check_call(["osascript", "-e", osa_step2])
    return left_id, right_id


async def session_by_id(connection, target_id: str):
    """Locate a session ref by exact ``unique id`` (case-insensitive)."""
    from teammate_mcp.iterm import list_sessions
    target_id_norm = target_id.strip().upper()
    refs = await list_sessions(connection)
    for r in refs:
        if r.session_id.upper() == target_id_norm:
            return r
    return None


async def wait_until_idle(connection, ref, idle_seconds: float = 4.0,
                          max_wait: float = 60.0) -> None:
    """Wait until the screen of ``ref`` stops changing for ``idle_seconds``."""
    deadline = time.monotonic() + max_wait
    last_screen = ""
    last_change = time.monotonic()
    while time.monotonic() < deadline:
        s = await get_screen(ref, n_lines=80)
        if s != last_screen:
            last_screen = s
            last_change = time.monotonic()
        elif time.monotonic() - last_change >= idle_seconds:
            return
        await asyncio.sleep(0.6)


async def main() -> int:
    timings: list[tuple[str, int]] = []
    record: dict = {
        "scenario": "auto_demo_claude_asks_codex",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }

    print("[demo] spawning iTerm window with claude+codex …")
    t0 = time.monotonic()
    claude_id, codex_id = spawn_window()
    print(f"[demo] new window panes: claude={claude_id}  codex={codex_id}")
    record["claude_pane_id"] = claude_id
    record["codex_pane_id"] = codex_id

    # Give the CLIs time to boot. claude+codex are heavyweight; at 25s
    # codex sometimes isn't ready to accept its first `\r`-submitted
    # prompt, so the demo silently times out.
    print("[demo] waiting 30s for both CLIs to boot")
    await asyncio.sleep(30.0)
    timings.append(("spawn_and_boot", int((time.monotonic() - t0) * 1000)))

    print("[demo] connecting to iTerm Python API")
    t = time.monotonic()
    connection = await iterm2.Connection.async_create()
    timings.append(("connect", int((time.monotonic() - t) * 1000)))

    try:
        # Resolve the panes by their *exact* unique ids returned from
        # AppleScript so we never confuse them with pre-existing claude
        # or codex instances elsewhere in iTerm.
        t = time.monotonic()
        claude_ref = await session_by_id(connection, claude_id)
        codex_ref = await session_by_id(connection, codex_id)
        timings.append(("locate_panes", int((time.monotonic() - t) * 1000)))

        if claude_ref is None or codex_ref is None:
            print("[demo] FAIL: spawned panes not visible to iterm2 lib yet:")
            refs = await list_sessions(connection)
            for r in refs:
                print(f"  {r.session_id}  job={r.job!r}  cwd={r.cwd!r}")
            return 2

        # Sanity check — they MUST be different.
        assert claude_ref.session_id.upper() != codex_ref.session_id.upper(), (
            "claude and codex resolved to the same session — abort"
        )
        print(f"[demo] found claude pane: {claude_ref.session_id}")
        print(f"[demo] found codex pane:  {codex_ref.session_id}")

        # Wait until both panes have stopped scrolling (their banners are done).
        print("[demo] waiting for prompts to settle")
        t = time.monotonic()
        await asyncio.gather(
            wait_until_idle(connection, claude_ref, idle_seconds=4.0, max_wait=60),
            wait_until_idle(connection, codex_ref, idle_seconds=4.0, max_wait=60),
        )
        timings.append(("prompts_settled", int((time.monotonic() - t) * 1000)))

        # Compose the test prompt. We pin a deterministic marker so we can
        # reliably detect Claude's final answer.
        sentinel = f"DEMO_DONE_{int(time.time()*1000)}"
        prompt = (
            f"Run this exact tool call right now: "
            f"mcp__teammate__ask_codex with question=\"What is two plus two? "
            f"Answer with the digit only.\" and timeout=120. "
            f"After the tool returns, write one short line summarising the "
            f"answer Codex gave you, then on the next line output exactly: "
            f"<<{sentinel}>>"
        )

        # NOTE: don't clear via ESC — ESC×2 triggers Claude Code's
        # "Rewind" menu and swallows the next prompt. Trust that the
        # freshly-spawned pane has no leftover input.
        print("[demo] injecting prompt into Claude pane")
        t_send = time.monotonic()
        await send_text(claude_ref, prompt)
        timings.append(("send_prompt", int((time.monotonic() - t_send) * 1000)))

        marker = f"<<{sentinel}>>"
        # We poll BOTH for Claude's terminating marker (preferred) AND
        # for an `ask.complete` event in the MCP log (which proves the
        # round trip happened even if Claude paraphrases instead of
        # emitting the literal marker). Either is sufficient evidence
        # that the system worked end-to-end.
        log_path = Path.home() / ".teammate-mcp" / "logs" / (
            datetime.now(timezone.utc).strftime("%Y-%m-%d") + ".jsonl"
        )
        log_offset = log_path.stat().st_size if log_path.exists() else 0
        ask_completed: dict | None = None

        print(f"[demo] waiting for marker {marker} ×2 OR ask.complete in MCP log "
              f"(timeout 240s)")
        t = time.monotonic()
        deadline = t + 240.0
        screen = None
        while time.monotonic() < deadline:
            # Check MCP log for ask.complete since our demo started.
            if log_path.exists():
                with log_path.open("r") as f:
                    f.seek(log_offset)
                    for line in f:
                        try:
                            ev = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if ev.get("event") == "ask.complete":
                            ask_completed = ev
                            break
                if ask_completed:
                    break
            screen = await wait_for_marker(
                claude_ref, marker, timeout=2.5, poll_interval=2.0, min_count=2
            )
            if screen is not None:
                break
        timings.append(("marker_or_log_signal",
                        int((time.monotonic() - t) * 1000)))

        codex_screen = await get_screen(codex_ref, n_lines=120)
        claude_screen = screen if screen is not None else await get_screen(claude_ref, n_lines=120)

        if screen is None and ask_completed is None:
            print("[demo] FAIL: no marker AND no ask.complete in MCP log within 240s")
            record.update({
                "status": "timeout",
                "phases": [{"name": n, "elapsed_ms": v} for n, v in timings],
                "claude_tail": claude_screen[-2000:],
                "codex_tail": codex_screen[-2000:],
            })
            _write_record(record)
            return 3

        if screen is not None:
            answer = extract_answer(screen, prompt, marker)
            evidence = "claude_marker"
        else:
            answer = "(MCP log confirmed round trip; Claude paraphrased instead of emitting literal marker)"
            evidence = "mcp_log_ask_complete"

        record.update({
            "status": "success",
            "evidence": evidence,
            "marker": marker,
            "ask_completed_event": ask_completed,
            "claude_answer_summary": answer,
            "claude_pane_tail": claude_screen[-1500:],
            "codex_pane_tail": codex_screen[-1500:],
            "phases": [{"name": n, "elapsed_ms": v} for n, v in timings],
            "total_ms": int((time.monotonic() - t0) * 1000),
        })
        _write_record(record)

        print()
        print("=== DEMO RESULT ===")
        print(f"status: success ({evidence})")
        print(f"timings: {timings}")
        if ask_completed:
            print(f"ask.complete: id={ask_completed.get('id')} "
                  f"answer_len={ask_completed.get('answer_len')}")
        print(f"claude summary: {answer[:500]}")
        print()
        return 0
    finally:
        try:
            connection.close()
        except Exception:
            pass


def _write_record(record: dict) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = RESULTS_DIR / f"{stamp}-auto_demo.jsonl"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2))
    print(f"[demo] log written → {path}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
