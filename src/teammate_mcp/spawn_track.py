"""Append-only ledger of session ids THIS project spawned.

Only entries written here are eligible for cleanup. Anything we merely
*touched* (e.g. via a misrouted ask, via auto-registration of a
pre-existing pane) is NOT in this file and must never be closed.

Format: one JSON line per spawn:
    {"ts": <unix>, "session_id": "...", "spawned_by": "<script-name>"}
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Iterable

LEDGER_PATH = Path.home() / ".teammate-mcp" / "spawned.jsonl"


def record(session_id: str, spawned_by: str = "unknown") -> None:
    """Append a spawn record. Idempotent: noop if already present."""
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    sid_up = session_id.strip().upper()
    if sid_up in load():
        return
    line = json.dumps({
        "ts": time.time(),
        "session_id": session_id,
        "spawned_by": spawned_by,
        "pid": os.getpid(),
    }, ensure_ascii=False)
    with LEDGER_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load() -> set[str]:
    """Return the set of session_ids we have ever spawned (uppercased)."""
    if not LEDGER_PATH.exists():
        return set()
    out: set[str] = set()
    with LEDGER_PATH.open() as f:
        for line in f:
            try:
                rec = json.loads(line)
                sid = rec.get("session_id")
                if sid:
                    out.add(sid.strip().upper())
            except json.JSONDecodeError:
                continue
    return out


def filter_to_known(session_ids: Iterable[str]) -> list[str]:
    """Filter input to only those we know we spawned."""
    known = load()
    out = []
    for sid in session_ids:
        if sid.strip().upper() in known:
            out.append(sid)
    return out
