#!/usr/bin/env python3
"""Large-payload ASK diagnostic.

Sends bodies of increasing size (1KB, 10KB, 50KB, 100KB, 250KB, 500KB,
1MB, 2MB) to a live Claude Code receiver and reports for each:
  - elapsed time
  - exit-status string (keystroke / file-fallback / TIMEOUT / ERROR)
  - whether it surfaced inside the receiver's screen

Identifies the size at which delivery breaks (or starts taking
unacceptably long).

PASS criteria per size: result == "keystroke" AND elapsed < 10s.
Whole script exits 0 if all PASS, 1 otherwise.
"""
from __future__ import annotations
import asyncio, os, subprocess, sys, time
from pathlib import Path

REPO_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(REPO_SRC))
import iterm2
from teammate_mcp import registry

BIN = "/Users/siheom-yong/programming/teammate-mcp/.venv/bin/teammate-mcp"


async def alive_python_pane(connection):
    L = registry.all_labels()
    app = await iterm2.async_get_app(connection)
    alive = set()
    for w in app.windows:
        for t in w.tabs:
            for s in t.sessions:
                alive.add(s.session_id.upper())
    for label, rec in L.items():
        if label in ("claude4", "claude8", "claude29"):
            continue
        if not label.startswith("claude"):
            continue
        if (rec.get("job") or "").lower() != "python":
            continue
        if rec.get("session_id", "").upper() in alive:
            return label
    return None


def make_body(size_bytes: int, marker: str) -> str:
    # Filler that's still readable in case it surfaces, with a leading
    # marker the receiver can echo back.
    filler = "abcdefghij" * 100  # 1KB chunk
    body = f"[size_test {marker}] "
    while len(body.encode("utf-8")) < size_bytes:
        body += filler
    return body[:size_bytes]


def humanize(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n // (1024*1024)}MB"
    if n >= 1024:
        return f"{n // 1024}KB"
    return f"{n}B"


async def amain():
    connection = await iterm2.Connection.async_create()
    target = await alive_python_pane(connection)
    if not target:
        print("no alive Claude Code receiver"); sys.exit(2)
    print(f"# receiver: {target}")
    print(f"{'size':>8}  {'elapsed':>9}  {'exit':>6}  result")
    print("-" * 60)

    sizes = [1024, 10*1024, 50*1024, 100*1024, 250*1024,
             500*1024, 1024*1024, 2*1024*1024]
    failures = []
    for sz in sizes:
        body = make_body(sz, f"sz{humanize(sz)}")
        t0 = time.monotonic()
        try:
            # Use --stdin to bypass ARG_MAX entirely.
            r = subprocess.run(
                [BIN, "ask", "--stdin", target],
                input=body,
                capture_output=True, text=True, timeout=60,
            )
            elapsed = time.monotonic() - t0
            stdout = r.stdout.strip()
            ec = r.returncode
            if "keystroke" in stdout:
                tag = "keystroke"
            elif "file-fallback" in stdout:
                tag = "file-fallback"
            elif "TIMEOUT" in stdout:
                tag = "TIMEOUT"
            else:
                tag = f"OTHER({stdout[:40]})"
            ok = (tag == "keystroke" and elapsed < 10)
            print(f"{humanize(sz):>8}  {elapsed:7.2f}s  {ec:>6}  {tag}  {'OK' if ok else 'FAIL'}")
            if not ok:
                failures.append((sz, elapsed, tag))
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - t0
            print(f"{humanize(sz):>8}  {elapsed:7.2f}s  -----  TIMEOUT_60s  FAIL")
            failures.append((sz, elapsed, "TIMEOUT_60s"))
            break  # don't try larger sizes if 60s exceeded
        # Sleep between sizes so receiver can clear
        await asyncio.sleep(2.0)

    print("-" * 60)
    if failures:
        print(f"FAIL — {len(failures)} sizes failed:")
        for sz, el, tag in failures:
            print(f"  {humanize(sz):>8}  {el:.2f}s  {tag}")
        sys.exit(1)
    print("PASS — all sizes delivered as keystroke under 10s")
    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(amain())
