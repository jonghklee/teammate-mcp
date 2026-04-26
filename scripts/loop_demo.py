"""Multi-case test loop for v0.2.

Each case spawns a fresh iTerm window with claude+codex panes and runs
a single round-trip. The label is auto-assigned by the MCP server (the
script never sets TEAMMATE_LABEL).

Cases:
  1. baseline  — both panes in the project root
  2. cross_dir — claude rooted in /tmp, codex rooted in project root
  3. baseline2 — repeat of case 1 to check stability across runs

For each case we record per-phase timings and the resulting registry
snapshot, then write a JSON line to tests/results/loop-<timestamp>.jsonl.
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

import iterm2

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from teammate_mcp.iterm import (  # noqa: E402
    extract_answer,
    find_pane,
    get_screen,
    list_sessions,
    send_text,
    wait_for_marker,
)
from teammate_mcp import registry  # noqa: E402
from teammate_mcp.server import auto_register_session  # noqa: E402

RESULTS_DIR = PROJECT_ROOT / "tests" / "results"
RESULTS_DIR.mkdir(exist_ok=True)
LOG_PATH = Path.home() / ".teammate-mcp" / "logs" / (
    datetime.now(timezone.utc).strftime("%Y-%m-%d") + ".jsonl"
)


def spawn_window(left_cwd: str, right_cwd: str) -> tuple[str, str]:
    """Open a fresh iTerm window split into [claude | codex]."""
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

    from teammate_mcp import spawn_track
    spawn_track.record(left_id, spawned_by="scripts/loop_demo.py")
    spawn_track.record(right_id, spawned_by="scripts/loop_demo.py")

    osa2 = f'''
tell application "iTerm"
    repeat with w in windows
        repeat with t in tabs of w
            repeat with s in sessions of t
                if (unique id of s) is "{left_id}" then
                    tell s
                        write text "cd {left_cwd}"
                        write text "claude --dangerously-skip-permissions"
                    end tell
                else if (unique id of s) is "{right_id}" then
                    tell s
                        write text "cd {right_cwd}"
                        write text "codex --yolo"
                    end tell
                end if
            end repeat
        end repeat
    end repeat
end tell
'''
    subprocess.check_call(["osascript", "-e", osa2])
    return left_id, right_id


async def session_by_id(connection, target_id: str):
    refs = await list_sessions(connection)
    for r in refs:
        if r.session_id.upper() == target_id.upper():
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


def log_events_since(offset: int) -> list[dict]:
    if not LOG_PATH.exists():
        return []
    events = []
    with LOG_PATH.open() as f:
        f.seek(offset)
        for line in f:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return events


async def run_one_case(case: dict) -> dict:
    name = case["name"]
    left_cwd = case["left_cwd"]
    right_cwd = case["right_cwd"]
    print(f"\n=== case: {name} ({left_cwd} / {right_cwd}) ===")

    timings: list[tuple[str, int]] = []
    t0 = time.monotonic()
    left_id, right_id = spawn_window(left_cwd, right_cwd)
    print(f"  panes spawned: claude={left_id[:8]}…  codex={right_id[:8]}…")

    # 30s minimum — at 25s codex isn't always ready to accept the first
    # `\r`-submitted prompt, so the round-trip silently times out.
    print("  waiting 30s for CLIs to boot")
    await asyncio.sleep(30.0)
    timings.append(("spawn_and_boot", int((time.monotonic() - t0) * 1000)))

    connection = await iterm2.Connection.async_create()
    try:
        claude_ref = await session_by_id(connection, left_id)
        codex_ref = await session_by_id(connection, right_id)
        assert claude_ref and codex_ref, "panes not found"
        print(f"  found both refs (jobNames: {claude_ref.job!r}, {codex_ref.job!r})")

        await asyncio.gather(
            wait_until_idle(claude_ref, idle=3.0),
            wait_until_idle(codex_ref, idle=3.0),
        )
        timings.append(("prompts_settled", int((time.monotonic() - t0) * 1000)))

        # Eagerly register both spawned panes — don't wait for the
        # CLIs to boot their MCP servers. This guarantees ask(target=…)
        # works on the very first turn.
        left_rec = await auto_register_session(connection, left_id)
        right_rec = await auto_register_session(connection, right_id)
        print(f"  registered: claude={left_rec}  codex={right_rec}")

        regsnap = registry.all_labels()
        print(f"  registry now: {list(regsnap.keys())}")

        codex_label = right_rec["label"] if right_rec else None
        claude_label = left_rec["label"] if left_rec else None
        print(f"  resolved labels: claude={claude_label!r} codex={codex_label!r}")

        sentinel = f"LOOP_{name}_{int(time.time()*1000)}"
        prompt = (
            f"Use mcp__teammate__ask with target={codex_label!r} (or fall back to "
            f"ask_codex if target is unknown), question="
            f'"What is two plus two? Answer with the digit only." and timeout=120. '
            f"After the tool returns, write one short line summarising the answer "
            f"Codex gave you, then on the next line output exactly: <<{sentinel}>>"
        )

        offset = log_offset()
        await send_text(claude_ref, prompt)
        timings.append(("send_prompt", int((time.monotonic() - t0) * 1000)))

        # Wait up to 240s for either: marker x2 in Claude pane OR
        # an ask.complete event whose timestamp is newer than `offset`.
        marker = f"<<{sentinel}>>"
        deadline = time.monotonic() + 240.0
        ask_complete_event = None
        screen = None
        while time.monotonic() < deadline:
            for ev in log_events_since(offset):
                if ev.get("event") == "ask.complete":
                    ask_complete_event = ev
                    break
            if ask_complete_event:
                break
            screen = await wait_for_marker(claude_ref, marker, timeout=2.0,
                                           poll_interval=2.0, min_count=2)
            if screen is not None:
                break
        timings.append(("round_trip", int((time.monotonic() - t0) * 1000)))

        result = {
            "case": name,
            "left_cwd": left_cwd,
            "right_cwd": right_cwd,
            "claude_pane_id": left_id,
            "codex_pane_id": right_id,
            "claude_label": next(
                (l for l, r in regsnap.items()
                 if r.get("session_id", "").upper() == left_id.upper()),
                None,
            ),
            "codex_label": codex_label,
            "registry_keys": sorted(regsnap.keys()),
            "ask_complete_event": ask_complete_event,
            "marker_seen_in_claude_pane": screen is not None,
            "phases": [{"name": n, "elapsed_ms": v} for n, v in timings],
            "status": "success" if (ask_complete_event or screen) else "timeout",
        }
        return result
    finally:
        try:
            connection.close()
        except Exception:
            pass


async def main():
    cases = [
        {"name": "baseline-A", "left_cwd": str(PROJECT_ROOT), "right_cwd": str(PROJECT_ROOT)},
        {"name": "cross-dir",  "left_cwd": "/tmp",            "right_cwd": str(PROJECT_ROOT)},
        {"name": "baseline-B", "left_cwd": str(PROJECT_ROOT), "right_cwd": str(PROJECT_ROOT)},
    ]
    all_results = []
    for case in cases:
        try:
            r = await run_one_case(case)
        except Exception as e:
            r = {"case": case["name"], "status": "exception", "error": repr(e)}
        all_results.append(r)
        print(f"  → {r.get('status')}  "
              f"(label: claude={r.get('claude_label')}, codex={r.get('codex_label')})")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = RESULTS_DIR / f"loop-{stamp}.jsonl"
    out_path.write_text(json.dumps(all_results, ensure_ascii=False, indent=2))
    print(f"\nLog: {out_path}")
    print("\n=== SUMMARY ===")
    for r in all_results:
        print(f"  {r['case']:<12} {r.get('status', '?'):<8}  "
              f"claude_label={r.get('claude_label')!s:<10} codex_label={r.get('codex_label')!s}")


if __name__ == "__main__":
    asyncio.run(main())
