#!/usr/bin/env python3
"""teammate-mcp UserPromptSubmit hook — drain inbox/ before each prompt.

Wired by ``bin/install-claude`` into ~/.claude/settings.json under
``hooks.UserPromptSubmit``. Runs in the receiver pane every time the
user (or another agent's keystroke injection in the SYNC path) submits
a prompt to Claude Code.

Behavior:
  1. Resolve THIS pane's teammate label by matching ``$TERM_SESSION_ID``
     against the registry at ``~/.teammate-mcp/registry.json``.
  2. Scan ``~/.teammate-mcp/mailbox/<label>/inbox/*.json`` in oldest-first
     order.
  3. For each pending message, emit a context block to stdout (Claude
     Code attaches the hook's stdout to the prompt as ``<additional-context>``)
     describing the ASK with its ``from_``, ``job_id``, and ``body``,
     plus a reminder to reply via reverse async ask.
  4. Move handled files to ``processed/`` so they aren't re-emitted.

If anything fails, exit silently with code 0 — never block the user's
prompt. The whole point of this hook is to be invisible until messages
arrive.
"""
import json
import os
import sys
from pathlib import Path

REGISTRY = Path.home() / ".teammate-mcp" / "registry.json"
MAILBOX = Path.home() / ".teammate-mcp" / "mailbox"
LOG = Path.home() / ".teammate-mcp" / "logs" / "hook-drain.log"


def _resolve_label() -> str:
    """Find this pane's label by matching TERM_SESSION_ID against
    the registry. Returns empty string if no match — the hook will
    then no-op silently.
    """
    # Explicit override wins.
    explicit = (os.environ.get("TEAMMATE_LABEL") or "").strip()
    if explicit:
        return explicit
    tsid = os.environ.get("TERM_SESSION_ID", "")
    if not tsid:
        return ""
    sid_tail = tsid.split(":", 1)[1] if ":" in tsid else tsid
    sid_tail = sid_tail.upper()
    if not sid_tail:
        return ""
    try:
        reg = json.loads(REGISTRY.read_text(encoding="utf-8"))
    except Exception:
        return ""
    for label, rec in reg.items():
        rec_sid = (rec.get("session_id") or "").upper()
        if rec_sid == sid_tail or rec_sid.endswith(sid_tail) or sid_tail.endswith(rec_sid):
            return label
    return ""


def _log(line: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line.rstrip() + "\n")
    except Exception:
        pass


def main() -> int:
    # Read (and discard) Claude Code's stdin JSON event. We don't need
    # any field from it; the hook's effect is purely additive context.
    try:
        sys.stdin.read()
    except Exception:
        pass

    label = _resolve_label()
    if not label:
        return 0

    inbox = MAILBOX / label / "inbox"
    processed = MAILBOX / label / "processed"
    if not inbox.exists():
        return 0

    files = sorted(inbox.glob("*.json"))
    if not files:
        return 0

    blocks = []
    for p in files:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            _log(f"skip {p.name}: {e}")
            continue
        sender = d.get("from_", "unknown")
        body = d.get("body", "")
        jid = d.get("job_id", "")
        blocks.append(
            f"[teammate-mcp inbox: ASK from={sender} job_id={jid}]\n"
            f"{body}\n"
            f"(To reply: `mcp__teammate__ask(target='{sender}', "
            f"question='<reply>', wait=False)` "
            f"or `teammate-mcp ask {sender} \"<reply>\" --no-wait` via Bash. "
            f"After replying, optionally call "
            f"mcp__teammate__mark_processed(job_id='{jid}', target='{label}', reply='<reply>') "
            f"to archive.)"
        )
        # Move to processed/ immediately so we don't re-emit on next prompt.
        try:
            processed.mkdir(parents=True, exist_ok=True)
            d.setdefault("delivered_via_hook_at", "now")
            (processed / p.name).write_text(
                json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8",
            )
            p.unlink(missing_ok=True)
        except Exception as e:
            _log(f"move-failed {p.name}: {e}")

    if not blocks:
        return 0

    out = (
        "════ teammate-mcp inbox (drained) ════\n"
        f"You ({label}) have {len(blocks)} pending message(s) from other panes. "
        f"Address them along with the user's request — for each, send a reply "
        f"via reverse async ask so the original sender's mailbox is updated.\n\n"
        + "\n────\n".join(blocks)
        + "\n════ end inbox ════\n"
    )
    print(out)
    _log(f"drained {len(blocks)} for label={label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
