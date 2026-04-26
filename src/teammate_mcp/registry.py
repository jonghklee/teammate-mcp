"""Persistent label → session_id registry.

Lives at ``~/.teammate-mcp/registry.json``. Each entry is owned by a single
process; we record its PID so stale entries (from killed CLIs) can be
auto-pruned on read.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional


REGISTRY_PATH = Path.home() / ".teammate-mcp" / "registry.json"


def _ensure_dir() -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _load_raw() -> dict:
    if not REGISTRY_PATH.exists():
        return {}
    try:
        return json.loads(REGISTRY_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_raw(data: dict) -> None:
    _ensure_dir()
    tmp = REGISTRY_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(REGISTRY_PATH)


def load() -> dict:
    """Load registry, pruning entries whose owning process is gone."""
    raw = _load_raw()
    pruned = {
        label: rec
        for label, rec in raw.items()
        if isinstance(rec, dict) and _alive(int(rec.get("pid", -1)))
    }
    if pruned != raw:
        _save_raw(pruned)
    return pruned


def register(
    label: str,
    session_id: str,
    pid: int,
    job: str,
    cwd: Optional[str] = None,
    extra: Optional[dict] = None,
) -> None:
    data = load()
    data[label] = {
        "label": label,
        "session_id": session_id,
        "pid": pid,
        "job": job,
        "cwd": cwd,
        "registered_at": time.time(),
        **(extra or {}),
    }
    _save_raw(data)


def unregister(label: str) -> None:
    data = load()
    data.pop(label, None)
    _save_raw(data)


def lookup(label: str) -> Optional[dict]:
    return load().get(label)


def all_labels() -> dict:
    return load()
