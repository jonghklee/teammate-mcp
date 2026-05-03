#!/usr/bin/env python3
"""Concurrent-ASK lag diagnostic.

Spawns two simultaneous `teammate-mcp ask <target>` invocations,
polls the receiver pane every 50ms, prints per-sender turnaround +
top no-change gaps + verdict (PASS / FAIL).

exit 0 = PASS (max gap < 1.5s)
exit 1 = FAIL
"""
from __future__ import annotations
import argparse, asyncio, hashlib, json, os, subprocess, sys, time
from pathlib import Path

REPO_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(REPO_SRC))
import iterm2  # noqa: E402
from teammate_mcp import registry  # noqa: E402

BIN = "/Users/siheom-yong/programming/teammate-mcp/.venv/bin/teammate-mcp"
PASS_GAP_THRESHOLD = 1.5


async def alive_sids(connection):
    app = await iterm2.async_get_app(connection)
    out = set()
    for w in app.windows:
        for t in w.tabs:
            for s in t.sessions:
                out.add(s.session_id.upper())
    return out


async def pick_receiver(connection, exclude=("claude4", "claude8", "claude29")):
    """Pick a Claude Code (job=Python) pane that's actually alive in iTerm.
    Skip shell-only panes (job=sh/zsh) — they don't process ASK bodies."""
    L = registry.all_labels()
    alive = await alive_sids(connection)
    for label, rec in L.items():
        if label in exclude or not label.startswith("claude"):
            continue
        if (rec.get("job") or "").lower() != "python":
            continue
        if rec.get("session_id", "").upper() in alive:
            return label
    return None


async def find_session(connection, sid):
    app = await iterm2.async_get_app(connection)
    for w in app.windows:
        for t in w.tabs:
            for s in t.sessions:
                if s.session_id == sid:
                    return s
    return None


async def run_diag(connection, target_label, body_a, body_b, poll, post):
    L = registry.all_labels()
    sid = L[target_label]["session_id"]
    target = await find_session(connection, sid)
    if not target:
        return {"error": "target session not found"}

    timeline = []
    stop_flag = {"v": False}
    t_zero = time.monotonic()

    async def snapshot_loop():
        last_hash = None
        while not stop_flag["v"]:
            try:
                contents = await target.async_get_screen_contents()
                n = contents.number_of_lines
                # Hash the whole visible buffer minus blank lines, so
                # we capture ASK echo / Mulling animation / response
                # text — not just status bar.
                lines = [contents.line(i).string.rstrip() for i in range(n)]
                content = '|'.join(l for l in lines if l.strip())
                h = hashlib.md5(content.encode("utf-8", "replace")).hexdigest()[:8]
                ts = time.monotonic() - t_zero
                if h != last_hash:
                    # capture preview = last meaningful 2 lines
                    preview = ' / '.join(l for l in lines[-15:] if l.strip())[-160:]
                    timeline.append((ts, h, preview))
                    last_hash = h
            except Exception as e:
                timeline.append((time.monotonic() - t_zero, 'ERR', repr(e)[:160]))
            await asyncio.sleep(poll)

    snap_task = asyncio.create_task(snapshot_loop())
    await asyncio.sleep(0.1)

    p1 = subprocess.Popen(
        [BIN, "ask", target_label, body_a],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "TEAMMATE_LABEL": "claude4"},
    )
    p2 = subprocess.Popen(
        [BIN, "ask", target_label, body_b],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "TEAMMATE_LABEL": "claude8"},
    )
    o1, _ = p1.communicate(timeout=30)
    elap_a = time.monotonic() - t_zero
    o2, _ = p2.communicate(timeout=30)
    elap_b = time.monotonic() - t_zero

    await asyncio.sleep(post)
    stop_flag["v"] = True
    snap_task.cancel()

    gaps = []
    for i in range(1, len(timeline)):
        gap = timeline[i][0] - timeline[i - 1][0]
        gaps.append((gap, timeline[i - 1][0], timeline[i - 1][2], timeline[i][2]))
    gaps.sort(reverse=True)

    return {
        "target": target_label,
        "frames": len(timeline),
        "sender_a_done_at": elap_a,
        "sender_b_done_at": elap_b,
        "sender_a_stdout": o1.decode().strip()[:160],
        "sender_b_stdout": o2.decode().strip()[:160],
        "top_gaps": gaps[:5],
        "max_gap": gaps[0][0] if gaps else 0.0,
    }


def classify_gap(before, after):
    blob = (before + after).lower()
    if "mulling" in blob or "sketching" in blob or "thinking" in blob:
        return "receiver-llm-thinking"
    if "[teammate-mcp ask" in blob:
        return "ask-injection"
    return "unknown"


async def amain():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target")
    ap.add_argument("--poll", type=float, default=0.05)
    ap.add_argument("--post", type=float, default=2.0)
    args = ap.parse_args()

    connection = await iterm2.Connection.async_create()
    target = args.target or await pick_receiver(connection)
    if not target:
        print(json.dumps({"error": "no alive receiver pane found"}))
        sys.exit(2)

    print(f"# diagnostic: 2 concurrent ASK -> {target}")
    r = await run_diag(connection, target, "diag A: 한 단어만",
                       "diag B: 한 단어만", args.poll, args.post)
    if "error" in r:
        print(json.dumps(r)); sys.exit(2)

    print(f"frames captured     : {r['frames']}")
    print(f"sender A finish     : T+{r['sender_a_done_at']:.2f}s  ({r['sender_a_stdout'][:60]})")
    print(f"sender B finish     : T+{r['sender_b_done_at']:.2f}s  ({r['sender_b_stdout'][:60]})")
    print(f"max receiver gap    : {r['max_gap']:.2f}s")
    print(f"\nTop-5 gaps:")
    for gap, ts, before, after in r["top_gaps"]:
        cls = classify_gap(before, after)
        print(f"  {gap:5.2f}s at T+{ts:5.2f}s  [{cls}]")
        print(f"      before: {before[:70]!r}")
        print(f"      after : {after[:70]!r}")

    verdict = "PASS" if r["max_gap"] < PASS_GAP_THRESHOLD else "FAIL"
    print(f"\nVERDICT: {verdict}  (threshold {PASS_GAP_THRESHOLD}s)")
    sys.exit(0 if verdict == "PASS" else 1)


if __name__ == "__main__":
    asyncio.run(amain())
