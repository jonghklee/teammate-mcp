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


# Cache of alive iTerm session IDs for prune_dead. Refreshed at most
# once per ``_PRUNE_TTL`` seconds — querying iTerm via osascript on
# every all_labels() call is too expensive (we call it from the hook).
_PRUNE_CACHE: dict = {"sids": frozenset(), "ts": 0.0}
_PRUNE_TTL = 5.0  # seconds


def _alive_session_ids_via_osascript() -> frozenset[str]:
    """One-shot AppleScript: list every iTerm session UUID currently open."""
    import subprocess
    script = '''
tell application "iTerm"
    set out to ""
    repeat with w in windows
        repeat with t in tabs of w
            repeat with s in sessions of t
                set out to out & (unique id of s) & "\n"
            end repeat
        end repeat
    end repeat
    return out
end tell
'''
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            check=True, capture_output=True, text=True, timeout=5,
        )
        return frozenset(line.strip().upper() for line in r.stdout.splitlines() if line.strip())
    except Exception:
        return frozenset()


def prune_dead(force_refresh: bool = False) -> list[str]:
    """Remove every registry entry whose iTerm session is no longer
    open. Returns the list of removed labels.

    Cheap: uses a single AppleScript call (≤200ms) cached for
    ``_PRUNE_TTL`` seconds, so calling this from list/register/lookup
    paths is fine.
    """
    now = time.monotonic()
    if force_refresh or (now - _PRUNE_CACHE["ts"] > _PRUNE_TTL):
        _PRUNE_CACHE["sids"] = _alive_session_ids_via_osascript()
        _PRUNE_CACHE["ts"] = now

    alive = _PRUNE_CACHE["sids"]
    if not alive:
        # iTerm not running, or AppleScript failed — don't risk
        # nuking the registry. No-op.
        return []

    removed: list[str] = []
    with _exclusive_lock():
        data = load()
        for label, rec in list(data.items()):
            sid = (rec.get("session_id") or "").strip().upper()
            if sid and sid not in alive:
                removed.append(label)
                data.pop(label, None)
        if removed:
            _save_raw(data)
    # Archive mailboxes of removed labels so their old inbox/processed
    # never bleeds into a future label collision (e.g. claude5 closed,
    # new pane later registered as claude5 — without this, the new
    # pane's hook would drain stale messages).
    if removed:
        try:
            from .server import archive_label_mailbox
            for label in removed:
                archive_label_mailbox(label)
        except Exception:
            pass
    return removed
