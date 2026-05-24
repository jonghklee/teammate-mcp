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
import subprocess
import time
from pathlib import Path
from typing import Optional


REGISTRY_PATH = Path.home() / ".teammate-mcp" / "registry.json"
LOCK_PATH = Path.home() / ".teammate-mcp" / "registry.lock"
ALIVE_CACHE_PATH = Path.home() / ".teammate-mcp" / "alive-sessions-cache.json"
ALIVE_CACHE_LOCK_PATH = Path.home() / ".teammate-mcp" / "alive-sessions-cache.lock"


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
    dedupe_session_id: bool = False,
) -> None:
    """Register a label → session_id mapping.

    When ``dedupe_session_id=True``, any *other* label currently pointing at
    the same ``session_id`` is removed. Prevents the claude3 ↔ codex11
    collision pattern where two labels share one iTerm session and the
    drain hook routes messages to the wrong one. Default is False to keep
    existing callers' behavior unchanged.
    """
    with _exclusive_lock():
        data = load()
        if dedupe_session_id and session_id:
            target_sid = session_id.upper()
            collisions = [
                l for l, r in data.items()
                if l != label and (r.get("session_id") or "").upper() == target_sid
            ]
            for l in collisions:
                data.pop(l, None)
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


# Process-local fallback cache of alive iTerm session IDs for prune_dead.
# The authoritative cache is file-backed so short-lived CLI/watchdog
# processes do not all stampede iTerm's AppleScript queue.
_PRUNE_CACHE: dict = {"sids": frozenset(), "ts": 0.0}
_PRUNE_TTL = 30.0  # seconds


def _alive_session_ids_via_osascript() -> frozenset[str]:
    """One-shot AppleScript: list every iTerm session UUID currently open."""
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


def alive_session_ids_cached(force_refresh: bool = False, ttl: float = _PRUNE_TTL) -> frozenset[str]:
    """Return alive iTerm session IDs using a shared file cache.

    Only the process that obtains ``ALIVE_CACHE_LOCK_PATH`` refreshes via
    osascript. Other concurrent callers use the existing cache or the
    process-local fallback. This keeps pane-count growth from turning into
    pane-count AppleScript calls.
    """
    now = time.time()
    try:
        raw = json.loads(ALIVE_CACHE_PATH.read_text(encoding="utf-8"))
        ts = float(raw.get("ts", 0.0))
        sids = frozenset(str(s).upper() for s in raw.get("sids", []) if s)
        if sids and not force_refresh and now - ts <= ttl:
            _PRUNE_CACHE["sids"] = sids
            _PRUNE_CACHE["ts"] = time.monotonic()
            return sids
    except Exception:
        ts = 0.0
        sids = frozenset()

    ALIVE_CACHE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(ALIVE_CACHE_LOCK_PATH), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.EACCES):
                raise
            if sids:
                return sids
            cached = _PRUNE_CACHE.get("sids") or frozenset()
            return cached

        # Another process may have refreshed between our first read and
        # acquiring the lock.
        try:
            raw = json.loads(ALIVE_CACHE_PATH.read_text(encoding="utf-8"))
            ts = float(raw.get("ts", 0.0))
            sids = frozenset(str(s).upper() for s in raw.get("sids", []) if s)
            if sids and not force_refresh and now - ts <= ttl:
                _PRUNE_CACHE["sids"] = sids
                _PRUNE_CACHE["ts"] = time.monotonic()
                return sids
        except Exception:
            pass

        refreshed = _alive_session_ids_via_osascript()
        if refreshed:
            ALIVE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = ALIVE_CACHE_PATH.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"ts": now, "sids": sorted(refreshed)}, indent=2),
                encoding="utf-8",
            )
            tmp.replace(ALIVE_CACHE_PATH)
            _PRUNE_CACHE["sids"] = refreshed
            _PRUNE_CACHE["ts"] = time.monotonic()
            return refreshed
        return sids or (_PRUNE_CACHE.get("sids") or frozenset())
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception:
            pass
        os.close(fd)


def prune_dead(
    force_refresh: bool = False,
    cache_ttl: float = _PRUNE_TTL,
    fresh_grace_s: float = 60.0,
) -> list[str]:
    """Remove every registry entry whose iTerm session is no longer
    open. Returns the list of removed labels.

    Session-id-based only — we deliberately do NOT prune on pid death.
    The recorded pid is the teammate-mcp MCP server child of
    claude/codex, and MCP servers may be recycled multiple times during
    a single pane's lifetime (especially codex, which can drop and
    respawn its MCP between turns). Pruning on pid_dead would erase the
    label seconds after the user registers a fresh pane.

    Labels younger than ``fresh_grace_s`` seconds are NEVER pruned even
    if their session_id isn't in the alive set yet. This protects newly
    spawned panes from a stale-cache race: ``alive_session_ids_cached``
    has a 30 s TTL, and iTerm's AppleScript reflection of a brand-new
    session can also lag a beat, so a label registered seconds ago might
    legitimately be missing from the cached alive set even though the
    pane exists. The grace window covers both delays without keeping
    truly dead labels around for any meaningful duration.

    The iTerm session_id is the source of truth (post-grace): pane open
    → label kept, pane closed → label removed. If a user kills the
    inner process but keeps the pane open as a zsh shell, the label
    survives until they close the pane or call
    ``teammate-mcp unregister`` manually.

    Cheap: uses a single AppleScript call (≤200ms) cached for
    ``_PRUNE_TTL`` seconds, so calling this from list/register/lookup
    paths is fine.
    """
    alive = alive_session_ids_cached(force_refresh=force_refresh, ttl=cache_ttl)
    if not alive:
        # iTerm not running, or AppleScript failed — don't risk
        # nuking the registry. No-op.
        return []

    removed: list[str] = []
    now = time.time()
    with _exclusive_lock():
        data = load()
        for label, rec in list(data.items()):
            sid = (rec.get("session_id") or "").strip().upper()
            if not sid:
                continue
            if sid in alive:
                continue
            registered_at = float(rec.get("registered_at") or 0.0)
            age = now - registered_at if registered_at else float("inf")
            if age < fresh_grace_s:
                # Just registered — give iTerm + alive cache time to catch up.
                continue
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
