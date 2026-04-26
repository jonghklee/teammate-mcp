"""Registry persistence + stale-PID pruning."""

from __future__ import annotations

import json
import os

import pytest

from teammate_mcp import registry


@pytest.fixture(autouse=True)
def _isolated_registry(tmp_path, monkeypatch):
    """Redirect ~/.teammate-mcp/registry.json to a per-test path."""
    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "registry.json")


def test_register_then_lookup_round_trip():
    registry.register(
        label="plan",
        session_id="ABC-123",
        pid=os.getpid(),
        job="claude",
        cwd="/tmp",
    )
    rec = registry.lookup("plan")
    assert rec is not None
    assert rec["session_id"] == "ABC-123"
    assert rec["job"] == "claude"


def test_unregister_removes_entry():
    registry.register("worker", "ZZZ", os.getpid(), "codex")
    assert registry.lookup("worker") is not None
    registry.unregister("worker")
    assert registry.lookup("worker") is None


def test_stale_pid_pruned_on_load():
    registry.register("plan", "ABC", os.getpid(), "claude")
    # Register a record with a PID that almost certainly isn't running.
    registry.register("ghost", "DEF", 999999, "codex")

    # Manually verify the file has both entries before load() prunes.
    raw = json.loads((registry.REGISTRY_PATH).read_text())
    assert "ghost" in raw

    loaded = registry.load()
    assert "plan" in loaded
    assert "ghost" not in loaded  # pruned


def test_all_labels_returns_live_only():
    registry.register("a", "111", os.getpid(), "claude")
    registry.register("b", "222", 999999, "codex")  # stale
    labels = registry.all_labels()
    assert set(labels.keys()) == {"a"}
