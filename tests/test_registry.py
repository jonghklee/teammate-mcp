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


def test_stale_pid_no_longer_pruned():
    """v0.3.2: PID-based pruning was removed because Codex's startup
    fork/exec was killing the PID we recorded a second earlier,
    silently losing the codex1 entry. Entries now persist until
    explicit unregister."""
    registry.register("plan", "ABC", os.getpid(), "claude")
    registry.register("ghost", "DEF", 999999, "codex")
    loaded = registry.load()
    assert "plan" in loaded
    assert "ghost" in loaded  # no longer pruned


def test_all_labels_returns_everything():
    registry.register("a", "111", os.getpid(), "claude")
    registry.register("b", "222", 999999, "codex")
    labels = registry.all_labels()
    assert set(labels.keys()) == {"a", "b"}
