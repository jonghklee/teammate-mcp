import importlib.util
import io
import json
import os
from pathlib import Path
import time
import pytest


@pytest.fixture(params=["user_prompt_submit_inbox_drain", "stop_inbox_drain"])
def hook(request, tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "hooks" / f"{request.param}.py"
    spec = importlib.util.spec_from_file_location(request.param, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    monkeypatch.setattr(module, "REGISTRY", tmp_path / "registry.json")
    monkeypatch.setattr(module, "MAILBOX", tmp_path / "mailbox")
    monkeypatch.setattr(module, "LOG", tmp_path / "hook.log")
    monkeypatch.setenv("TERM_SESSION_ID", "w0:CLAUDE-PANE")
    monkeypatch.delenv("TEAMMATE_LABEL", raising=False)
    module.REGISTRY.write_text(json.dumps({"codex": {"session_id": ""}, "claude": {"session_id": "CLAUDE-PANE"}}))
    monkeypatch.setattr(module.sys, "stdin", io.StringIO('{}'))
    return module


def test_hook_does_not_match_empty_mailbox_session_id(hook):
    assert hook._resolve_label() == "claude"


def test_hook_skips_claimed_message_but_drains_other_message(hook, capsys):
    inbox = hook.MAILBOX / "claude" / "inbox"; inbox.mkdir(parents=True)
    for job, extra in [('claimed', {"delivery_lease": {"pid": os.getpid(), "expires_at": time.time() + 60}}), ('free', {})]:
        (inbox / f"{job}.json").write_text(json.dumps({"job_id": job, "from_": "sender", "body": job, **extra}))
    assert hook.main() == 0
    output = capsys.readouterr().out
    assert "free" in output and "claimed" not in output
    assert (inbox / "claimed.json").exists()
    assert not (inbox / "free.json").exists()
