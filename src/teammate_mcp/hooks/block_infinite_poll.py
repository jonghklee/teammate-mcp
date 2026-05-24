"""Claude Code PreToolUse guard for runaway teammate-mcp polling loops."""

from __future__ import annotations

import json
import re
import sys


class Decision:
    def __init__(self, blocked: bool, reason: str = "") -> None:
        self.blocked = blocked
        self.reason = reason


_DYNAMIC_NOW_PLUS_RE = re.compile(
    r"date\s+\+%s.*date\s+\+%s\s*\)\s*\+\s*\d+",
    re.DOTALL,
)
_UNBOUNDED_LOOP_RE = re.compile(r"\bwhile\s+(?:true|:)\s*;", re.IGNORECASE)
_UNTIL_LOOP_RE = re.compile(r"\buntil\b.+\bdo\b", re.IGNORECASE | re.DOTALL)
_HAS_SLEEP_RE = re.compile(r"\bsleep\s+\d", re.IGNORECASE)
_TEAMMATE_POLL_RE = re.compile(
    r"\bteammate-mcp\s+(?:drain|inbox|ask|watch)\b",
    re.IGNORECASE,
)


def evaluate_command(command: str) -> Decision:
    """Return whether a Bash command should be blocked.

    Targets the incident pattern where an LLM creates a shell polling
    loop around teammate-mcp and the loop can outlive Claude as a PPID=1
    orphan. Bounded for-loops and single-shot teammate-mcp commands are
    allowed.
    """
    if not command:
        return Decision(False)
    if not _TEAMMATE_POLL_RE.search(command):
        return Decision(False)
    if not _HAS_SLEEP_RE.search(command):
        return Decision(False)

    if _DYNAMIC_NOW_PLUS_RE.search(command):
        return Decision(True, "dynamic timeout recalculates now+N on every loop")
    if _UNBOUNDED_LOOP_RE.search(command):
        return Decision(True, "unbounded polling loop")
    if _UNTIL_LOOP_RE.search(command) and "date +%s" in command:
        return Decision(True, "until polling loop with shell-computed timeout")
    return Decision(False)


def _extract_command(payload: dict) -> str:
    tool_input = payload.get("tool_input") or payload.get("input") or {}
    if isinstance(tool_input, dict):
        cmd = tool_input.get("command") or ""
        return cmd if isinstance(cmd, str) else ""
    return ""


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0

    decision = evaluate_command(_extract_command(payload))
    if not decision.blocked:
        return 0

    print(
        "Blocked teammate-mcp polling loop: "
        f"{decision.reason}. Use a bounded loop with a fixed deadline "
        "variable, or a single `teammate-mcp drain`/`inbox` command.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
