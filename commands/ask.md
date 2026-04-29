---
description: "Ask another registered pane via the teammate-mcp CLI (bypasses MCP tool to skip deferred-schema load + extended-thinking overhead). Default ASYNC. Usage: /ask <label> <question...>"
---

Parse the slash arguments. The first non-flag token is the target
label (e.g. `claude1`, `codex1`, `worker`). Everything after it is the
question, taken verbatim.

**As of v0.8.0 the default is ASYNC** (mailbox / file-only delivery).
The caller is never blocked, and the target's compose box / interactive
bash / permission prompts are never corrupted by injected keystrokes.

Supported flags (anywhere in the args):
- `--wait`: legacy SYNC mode — inject keystrokes + poll the target's
  screen for a completion marker. Use only when the caller cannot
  proceed without the inline reply. Will MERGE with text the user is
  mid-typing in the target compose box, so prefer the default.
- `--async` / `--no-wait`: explicit async (default; included for clarity).
- `--timeout N`: sync timeout in seconds (default 300; ignored when async).

**Run this exact Bash command** — do NOT call `mcp__teammate__ask`,
which is the whole point of this slash command. The MCP tool path
incurs:
- a deferred-schema `ToolSearch` round-trip on the first call per
  session (1–3 s),
- LLM extended-thinking time deciding to route to the tool (10–40 s
  on Opus with thinking enabled).

The CLI bypasses both:

```bash
teammate-mcp ask <LABEL> "<QUESTION>"          # async (default)
teammate-mcp ask --wait <LABEL> "<QUESTION>"   # legacy sync
```

Substitute `<LABEL>` and `<QUESTION>`; escape any literal `"` inside
the question as `\"`. If `teammate-mcp` is not on PATH, fall back to
the absolute venv path (`<repo>/.venv/bin/teammate-mcp`).

Print the command's stdout back to the user verbatim, then end the
turn. Do not summarise, do not add commentary, do not call any other
tool.

If the user did not supply both a label and a question, print:
`Usage: /ask [--async] <label> <question...>` and end the turn.
