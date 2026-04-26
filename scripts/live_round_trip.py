"""End-to-end round-trip via the v0.4 CLI register flow.

What it proves:
  1. Two iTerm panes are spawned, each running the CLI register
     command BEFORE launching claude/codex (no LLM in the loop for
     registration).
  2. The registry shows both panes labelled claude1 / codex1.
  3. A prompt injected into the claude1 pane causes Claude to call
     `mcp__teammate__ask(target="codex1", ...)`.
  4. codex1 receives the prompt, replies with "4" + the marker.
  5. The MCP server logs `ask.complete`.

If `ask.complete` lands within the deadline, the round-trip is proven
end-to-end. Captures both panes' tails for inspection. Records every
spawned session_id in the spawn ledger so cleanup_my_panes.py can
close ONLY the windows we created.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import iterm2  # noqa: E402
from teammate_mcp.iterm import (  # noqa: E402
    extract_answer,
    get_screen,
    list_sessions,
    send_text,
    wait_for_marker,
)
from teammate_mcp import registry, spawn_track  # noqa: E402

RESULTS_DIR = PROJECT_ROOT / "tests" / "results"
RESULTS_DIR.mkdir(exist_ok=True)
LOG_PATH = Path.home() / ".teammate-mcp" / "logs" / (
    datetime.now(timezone.utc).strftime("%Y-%m-%d") + ".jsonl"
)
TMCLAUDE = str(PROJECT_ROOT / "bin" / "tmclaude")
TMCODEX = str(PROJECT_ROOT / "bin" / "tmcodex")


def spawn_pair() -> tuple[str, str]:
    """Open one iTerm window split into [tmclaude | tmcodex].

    Each pane runs `register-pane` BEFORE launching its CLI, so the
    registry is populated before the model has a chance to do
    anything.
    """
    osa1 = '''
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
    out = subprocess.check_output(["osascript", "-e", osa1], text=True).strip()
    left_id, right_id = out.split("|")
    spawn_track.record(left_id, spawned_by="scripts/live_round_trip.py")
    spawn_track.record(right_id, spawned_by="scripts/live_round_trip.py")

    cwd = str(PROJECT_ROOT)
    osa2 = f'''
tell application "iTerm"
    repeat with w in windows
        repeat with t in tabs of w
            repeat with s in sessions of t
                if (unique id of s) is "{left_id}" then
                    tell s
                        write text "cd {cwd}"
                        write text "{TMCLAUDE}"
                    end tell
                else if (unique id of s) is "{right_id}" then
                    tell s
                        write text "cd {cwd}"
                        write text "{TMCODEX}"
                    end tell
                end if
            end repeat
        end repeat
    end repeat
end tell
'''
    subprocess.check_call(["osascript", "-e", osa2])
    return left_id, right_id


async def session_by_id(connection, sid: str):
    refs = await list_sessions(connection)
    for r in refs:
        if r.session_id.upper() == sid.upper():
            return r
    return None


async def wait_until_idle(ref, idle: float = 4.0, max_wait: float = 60.0) -> None:
    deadline = time.monotonic() + max_wait
    last = ""
    last_change = time.monotonic()
    while time.monotonic() < deadline:
        s = await get_screen(ref, n_lines=80)
        if s != last:
            last = s
            last_change = time.monotonic()
        elif time.monotonic() - last_change >= idle:
            return
        await asyncio.sleep(0.6)


def log_offset() -> int:
    return LOG_PATH.stat().st_size if LOG_PATH.exists() else 0


def events_since(offset: int) -> list[dict]:
    if not LOG_PATH.exists():
        return []
    out = []
    with LOG_PATH.open() as f:
        f.seek(offset)
        for line in f:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


async def main() -> int:
    print("=== teammate-mcp live round-trip ===\n")
    timings: list[tuple[str, int]] = []
    t0 = time.monotonic()

    print("[1] spawning iTerm window with tmclaude | tmcodex")
    left, right = spawn_pair()
    print(f"    left={left[:8]}…   right={right[:8]}…")

    print("[2] waiting 35s for both wrappers to (a) register-pane via CLI, "
          "(b) launch claude/codex, (c) reach prompt-ready state")
    await asyncio.sleep(35.0)
    timings.append(("spawn_and_boot", int((time.monotonic() - t0) * 1000)))

    connection = await iterm2.Connection.async_create()
    try:
        claude_ref = await session_by_id(connection, left)
        codex_ref = await session_by_id(connection, right)
        if not claude_ref or not codex_ref:
            print("ERROR: panes not found after spawn")
            return 2

        # Snapshot registry — both should be present.
        regsnap = registry.all_labels()
        print(f"\n[3] registry snapshot: {sorted(regsnap.keys())}")
        claude_label = next(
            (l for l, r in regsnap.items()
             if (r.get("session_id") or "").upper() == left.upper()), None,
        )
        codex_label = next(
            (l for l, r in regsnap.items()
             if (r.get("session_id") or "").upper() == right.upper()), None,
        )
        print(f"    left  → label={claude_label!r}")
        print(f"    right → label={codex_label!r}")
        if not codex_label:
            print("ERROR: codex pane was not registered by the CLI wrapper. "
                  "Check ~/.teammate-mcp/registry.json and /tmp/teammate logs.")
            return 3

        print("\n[4] waiting for both prompts to settle")
        await asyncio.gather(
            wait_until_idle(claude_ref, idle=3.0),
            wait_until_idle(codex_ref, idle=3.0),
        )
        timings.append(("prompts_settled", int((time.monotonic() - t0) * 1000)))

        # Inject a prompt into the claude pane that will (1) call ask
        # against the codex_label, (2) end with our sentinel marker so
        # we can detect Claude's reply.
        sentinel = f"DEMO_DONE_{int(time.time()*1000)}"
        prompt = (
            f"Use mcp__teammate__ask with target={codex_label!r} and "
            f"question=\"What is two plus two? Answer with the digit only.\" "
            f"and timeout=120. After the tool returns, write one short line "
            f"summarising the answer Codex gave you, then on the next line "
            f"output exactly: <<{sentinel}>>"
        )

        print(f"\n[5] injecting prompt into claude1 (target={codex_label})")
        offset = log_offset()
        await send_text(claude_ref, prompt)
        timings.append(("send_prompt", int((time.monotonic() - t0) * 1000)))

        # Wait for either Claude's marker (×2) or an ask.complete event
        # in the MCP log since `offset`.
        print(f"[6] waiting up to 240s for ask.complete OR Claude marker")
        deadline = time.monotonic() + 240.0
        ask_complete = None
        while time.monotonic() < deadline:
            for ev in events_since(offset):
                if ev.get("event") == "ask.complete":
                    ask_complete = ev
                    break
            if ask_complete:
                break
            await asyncio.sleep(2.0)
        timings.append(("complete_or_timeout", int((time.monotonic() - t0) * 1000)))

        # Capture both panes for the result file.
        claude_tail = await get_screen(claude_ref, n_lines=80)
        codex_tail = await get_screen(codex_ref, n_lines=80)

        record = {
            "scenario": "live_round_trip",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "claude_pane_id": left,
            "codex_pane_id": right,
            "claude_label": claude_label,
            "codex_label": codex_label,
            "ask_complete_event": ask_complete,
            "phases": [{"name": n, "elapsed_ms": v} for n, v in timings],
            "claude_tail": claude_tail[-2000:],
            "codex_tail": codex_tail[-2000:],
            "status": "success" if ask_complete else "timeout",
        }
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_path = RESULTS_DIR / f"live-{stamp}.jsonl"
        out_path.write_text(json.dumps(record, ensure_ascii=False, indent=2))

        print(f"\n=== RESULT ===")
        print(f"status: {record['status']}")
        if ask_complete:
            print(f"ask.complete: id={ask_complete.get('id')} "
                  f"answer_len={ask_complete.get('answer_len')}")
        print(f"timings: {timings}")
        print(f"log: {out_path}")
        print(f"\npanes left OPEN for inspection — close manually when done")
        return 0 if ask_complete else 1
    finally:
        try:
            connection.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
