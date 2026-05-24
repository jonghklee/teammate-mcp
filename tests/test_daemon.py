"""Unit tests for daemon request handlers."""

from __future__ import annotations

from teammate_mcp import daemon, registry


def test_daemon_register_dedupes_session_id(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "registry.json")
    registry.register("old", "SID-1", 111, "claude")

    result = daemon._op_register({
        "label": "new",
        "session_id": "SID-1",
        "pid": 222,
        "job": "codex",
        "cwd": "/tmp",
        "extra": {"via": "test"},
    })

    labels = registry.all_labels()
    assert result == {"label": "new", "ok": True}
    assert "old" not in labels
    assert labels["new"]["job"] == "codex"
    assert labels["new"]["via"] == "test"


def test_daemon_lookup_matches_label_and_session_prefix(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "registry.json")
    registry.register("worker", "ABCDEF-123", 111, "claude")

    assert daemon._op_lookup({"target": "worker"})["session_id"] == "ABCDEF-123"
    assert daemon._op_lookup({"target": "ABCDEF"})["label"] == "worker"
    assert daemon._op_lookup({"target": "missing"}) is None


def test_daemon_health_reports_process_and_label_count(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "REGISTRY_PATH", tmp_path / "registry.json")
    monkeypatch.setattr(daemon, "_started_at", 100.0)
    monkeypatch.setattr(daemon.time, "time", lambda: 112.5)
    registry.register("one", "SID", 111, "claude")

    health = daemon._op_health({})

    assert health["uptime_s"] == 12.5
    assert health["label_count"] == 1
    assert health["version"]
