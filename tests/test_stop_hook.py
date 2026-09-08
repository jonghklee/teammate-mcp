"""Unit tests for the Stop-hook inbox drain (deterministic ping-pong).

The hook ships as a standalone script (hooks/stop_inbox_drain.py) so it
can run via `python3 hook.py` without the package installed; load it by
path here and exercise the pure decide() logic.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_HOOK = Path(__file__).resolve().parent.parent / "hooks" / "stop_inbox_drain.py"


@pytest.fixture(scope="module")
def hook():
    spec = importlib.util.spec_from_file_location("stop_inbox_drain", _HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rec(sender="evalworker2", jid="77", body="ack 살아있음"):
    return {"from_": sender, "job_id": jid, "body": body}


def test_allows_stop_when_inbox_empty(hook):
    assert hook.decide([], stop_hook_active=False) is None


def test_allows_stop_when_already_continued_once(hook):
    # Loop guard: never block twice in a row.
    assert hook.decide([_rec()], stop_hook_active=True) is None


def test_blocks_and_surfaces_pending_messages(hook):
    out = hook.decide([_rec()], stop_hook_active=False, label="claude3")
    assert out is not None
    assert out["decision"] == "block"
    assert "evalworker2" in out["reason"]
    assert "ack 살아있음" in out["reason"]
    assert "claude3" in out["reason"]
    # The reply references the received question, preserving correlation.
    assert "mcp__teammate__reply(job_id='77'" in out["reason"]


def test_block_reason_caps_attachments(hook):
    records = [_rec(sender=f"w{i}", jid=str(i), body=f"msg{i}") for i in range(10)]
    out = hook.decide(records, stop_hook_active=False, max_attach=3, label="claude3")
    assert out["decision"] == "block"
    # Only the first 3 bodies are surfaced.
    assert "msg0" in out["reason"] and "msg2" in out["reason"]
    assert "msg3" not in out["reason"]
