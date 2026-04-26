"""Persistent label → session_id registry.

Lives at ``~/.teammate-mcp/registry.json``. Each entry is owned by a single
process; we record its PID so stale entries (from killed CLIs) can be
auto-pruned on read.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import time
from pathlib import Path
from typing import Optional


REGISTRY_PATH = Path.home() / ".teammate-mcp" / "registry.json"
LOCK_PATH = Path.home() / ".teammate-mcp" / "registry.lock"


@contextlib.contextmanager
def _exclusive_lock():
    """Cross-process exclusive lock for registry mutations.

    Without this, two simultaneous CLI register-pane calls can race
    on the read-modify-write of registry.json and lose one entry.
    """
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(LOCK_PATH), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + 10.0
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EACCES):
                    raise
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


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
    """Load registry. No PID-based pruning — that gave false positives
    when CLIs (e.g. Codex) fork/exec themselves at startup, killing the
    PID we recorded a second earlier. Entries are removed only via
    explicit ``unregister`` or ``cleanup_my_panes.py`` (which uses the
    spawn ledger, not PID liveness)."""
    raw = _load_raw()
    return {
        label: rec
        for label, rec in raw.items()
        if isinstance(rec, dict)
    }


def register(
    label: str,
    session_id: str,
    pid: int,
    job: str,
    cwd: Optional[str] = None,
    extra: Optional[dict] = None,
) -> None:
    with _exclusive_lock():
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
    with _exclusive_lock():
        data = load()
        data.pop(label, None)
        _save_raw(data)


def lookup(label: str) -> Optional[dict]:
    return load().get(label)


def all_labels() -> dict:
    return load()
