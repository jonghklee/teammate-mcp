"""Unit tests for mailbox reply matching helpers."""

from __future__ import annotations

import json

from teammate_mcp import cli
from teammate_mcp import server


def _write_msg(root, box, subdir, job_id, sender, body, created_at):
    path = root / box / subdir
    path.mkdir(parents=True, exist_ok=True)
    (path / f"{job_id}.json").write_text(
        json.dumps(
            {
                "job_id": job_id,
                "from_": sender,
                "to": box,
                "body": body,
                "created_at": created_at,
                "status": "queued",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_next_reply_matches_oldest_sender_from_inbox_and_processed(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(server, "MAILBOX_ROOT", tmp_path / "mailbox")
    _write_msg(
        server.MAILBOX_ROOT,
        "codex1",
        "inbox",
        "later",
        "round-codex-1",
        "round-codex-1:서울",
        "2026-05-14T00:09:05.626Z",
    )
    _write_msg(
        server.MAILBOX_ROOT,
        "codex1",
        "processed",
        "earlier",
        "round-claude-1",
        "round-claude-1:5",
        "2026-05-14T00:08:55.681Z",
    )

    rc = cli._cmd_next_reply(["codex1", "round-claude-1", "round-codex-1"])

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["from_"] == "round-claude-1"
    assert out["body"] == "round-claude-1:5"
    assert out["job_id"] == "earlier"


def test_next_reply_can_match_sender_prefix_in_body(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(server, "MAILBOX_ROOT", tmp_path / "mailbox")
    _write_msg(
        server.MAILBOX_ROOT,
        "codex1",
        "inbox",
        "body-prefix",
        "unknown",
        "round-claude-2:사",
        "2026-05-14T00:08:59.166Z",
    )

    rc = cli._cmd_next_reply(["codex1", "round-claude-2"])

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["matched_label"] == "round-claude-2"
    assert out["body"] == "round-claude-2:사"


def test_next_reply_consume_deletes_matched_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(server, "MAILBOX_ROOT", tmp_path / "mailbox")
    _write_msg(
        server.MAILBOX_ROOT,
        "codex1",
        "inbox",
        "consume-me",
        "round-codex-1",
        "round-codex-1:서울",
        "2026-05-14T00:09:05.626Z",
    )

    rc = cli._cmd_next_reply(["--consume", "codex1", "round-codex-1"])

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["matched_label"] == "round-codex-1"
    assert not (server.MAILBOX_ROOT / "codex1" / "inbox" / "consume-me.json").exists()


def test_next_reply_returns_one_when_no_match(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(server, "MAILBOX_ROOT", tmp_path / "mailbox")
    _write_msg(
        server.MAILBOX_ROOT,
        "codex1",
        "inbox",
        "other",
        "someone-else",
        "hello",
        "2026-05-14T00:09:05.626Z",
    )

    rc = cli._cmd_next_reply(["codex1", "round-codex-1"])

    assert rc == 1
    out = capsys.readouterr().out.strip()
    assert out == "(no matching reply)"
