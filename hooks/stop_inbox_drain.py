#!/usr/bin/env python3
"""teammate-mcp Stop hook — deterministic ping-pong / report-back.

Wired into ~/.claude/settings.json under ``hooks.Stop``. Fires when a
Claude pane FINISHES a turn. If this pane has pending teammate mail, it
blocks the stop and feeds the messages back so Claude processes them
*without* waiting for the user to type — the deterministic counterpart
to the watcher's screen-scrape wake (which a perpetually-busy pane can
starve forever).

Flow:
  1. Read the Stop event JSON on stdin (for ``stop_hook_active``).
  2. Resolve THIS pane's label (TERM_SESSION_ID → registry, or
     TEAMMATE_LABEL override).
  3. If the inbox has pending messages AND we are not already inside a
     Stop-hook continuation, move them to ``processed/`` and emit
     ``{"decision":"block","reason": <messages>}`` so Claude keeps going.
  4. Otherwise print nothing (allow the stop).

Loop safety:
  - ``stop_hook_active`` (set by Claude Code when the current stop is
    itself the result of a previous Stop-hook block) short-circuits to
    "allow stop", so we never block twice in a row.
  - Surfaced messages are moved to ``processed/`` so the *next* Stop sees
    an empty inbox.

Fails open: any error → exit 0 with no output (never wedge a session).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

REGISTRY = Path.home() / ".teammate-mcp" / "registry.json"
MAILBOX = Path.home() / ".teammate-mcp" / "mailbox"
LOG = Path.home() / ".teammate-mcp" / "logs" / "hook-stop-drain.log"
DEFAULT_MAX_ATTACH = 5


def _log(msg: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(msg.rstrip() + "\n")
    except Exception:
        pass


def _resolve_label() -> str:
    """This pane's label: TEAMMATE_LABEL override, else TERM_SESSION_ID
    matched against the registry. Empty string → hook no-ops."""
    explicit = (os.environ.get("TEAMMATE_LABEL") or "").strip()
    if explicit:
        return explicit
    tsid = os.environ.get("TERM_SESSION_ID", "")
    sid_tail = (tsid.split(":", 1)[1] if ":" in tsid else tsid).upper()
    if not sid_tail:
        return ""
    try:
        reg = json.loads(REGISTRY.read_text(encoding="utf-8"))
    except Exception:
        return ""
    labels = reg.get("labels", reg) if isinstance(reg, dict) else {}
    for label, rec in (labels.items() if isinstance(labels, dict) else []):
        rec_sid = (rec.get("session_id") or "").upper()
        if rec_sid and (rec_sid == sid_tail or rec_sid.endswith(sid_tail)
                        or sid_tail.endswith(rec_sid)):
            return label
    return ""


def _format_reason(label: str, records: list[dict]) -> str:
    """Render pending inbox records into the ``reason`` Claude continues on."""
    blocks = []
    for d in records:
        sender = d.get("from_", "unknown")
        jid = d.get("job_id", "")
        body = d.get("body", "")
        instr = (
            f"Reply via mcp__teammate__ask(target='{sender}', question='<reply>') "
            f"— not Bash, not XML tool tags."
        )
        blocks.append(
            f"[teammate-mcp inbox: ASK from={sender} job_id={jid}]\n{body}\n({instr})"
        )
    return (
        f"You ({label}) have {len(records)} pending teammate message(s) that "
        f"arrived while you were working. Process each and reply via reverse "
        f"async ask before stopping.\n\n"
        + "\n────\n".join(blocks)
    )


def decide(records: list[dict], stop_hook_active: bool,
           max_attach: int = DEFAULT_MAX_ATTACH, label: str = "me") -> Optional[dict]:
    """Pure decision. Returns the hook-output dict to print, or None to
    allow the stop (print nothing).

    - ``stop_hook_active`` True  → None (already continued once; avoid loop)
    - no pending records         → None (nothing to do)
    - otherwise                  → {"decision":"block","reason": ...}
    """
    if stop_hook_active:
        return None
    if not records:
        return None
    selected = records[:max_attach]
    return {"decision": "block", "reason": _format_reason(label, selected)}


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        payload = {}
    stop_hook_active = bool(payload.get("stop_hook_active"))

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

    records: list[dict] = []
    for p in files:
        try:
            records.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            records.append({})

    out = decide(records, stop_hook_active, label=label)
    if out is None:
        return 0

    # We are going to block + surface the messages: move them to
    # processed/ so the next Stop sees an empty inbox (no re-block loop).
    n = len(out["reason"])  # for log only
    moved = 0
    for p in files[:DEFAULT_MAX_ATTACH]:
        try:
            processed.mkdir(parents=True, exist_ok=True)
            (processed / p.name).write_text(p.read_text(encoding="utf-8"),
                                            encoding="utf-8")
            p.unlink(missing_ok=True)
            moved += 1
        except Exception as e:
            _log(f"move-failed {p.name}: {e}")
    print(json.dumps(out, ensure_ascii=False))
    _log(f"stop-drain blocked label={label} surfaced={moved} reason_len={n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
