"""Shared lightweight claim checks for pane senders, hooks and watchdog."""
import os
import time
from datetime import datetime
from contextlib import contextmanager
import fcntl
import json


def legacy_input_enabled():
    """Keyboard transport is opt-in; native delivery never touches a draft."""
    return os.environ.get("TEAMMATE_LEGACY_PANE_INPUT", "").lower() in ("1", "true", "yes")


def lease_active(record, now=None):
    lease = record.get("delivery_lease") or {}
    now = time.time() if now is None else now
    try:
        if float(lease.get("expires_at", 0)) <= now:
            return False
        pid = int(lease.get("pid", 0))
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError):
        return False


def eligible_for_watch(record, now=None):
    if record.get("delivery_mode") == "claude-channel":
        return False
    if record.get("pane_delivery", {}).get("state") in ("dispatching", "submitted", "uncertain"):
        return False
    if lease_active(record, now):
        return False
    # Old running senders have no lease. Give their direct injection a small
    # grace period until they reconnect to this version.
    if "delivery_mode" not in record and record.get("created_at"):
        try:
            created = datetime.fromisoformat(record["created_at"].replace("Z", "+00:00")).timestamp()
            return (time.time() if now is None else now) - created >= 15
        except (ValueError, TypeError):
            return False
    return True


def available_to_hook(path):
    try:
        record = json.loads(path.read_text())
        if record.get("delivery_mode") == "claude-channel":
            return False
        return not lease_active(record) and record.get("pane_delivery", {}).get("state") not in ("dispatching", "submitted", "uncertain")
    except (OSError, ValueError):
        return False


@contextmanager
def hook_delivery_lock(mailbox_root, label):
    directory = mailbox_root / label
    if not directory.exists():
        yield False
        return
    with (directory / ".send-lock").open("a") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
