---
description: "Ask another registered pane via the teammate-mcp CLI (bypasses MCP tool to skip deferred-schema load + extended-thinking overhead). Usage: /ask [--async] <label> <question...>"
---

Parse the slash arguments. The first non-flag token is the target
label (e.g. `claude1`, `codex1`, `worker`). Everything after it is the
question, taken verbatim.

Supported flags (anywhere before the label):
- `--async` / `--no-wait`: fire-and-forget mailbox mode. Caller is not
  blocked; target replies via a reverse `ask` (email model). Use this
  for "send and continue" — long tasks, background notifications,
  multi-pane choreography.
- `--timeout N`: sync timeout in seconds (default 300; ignored with
  `--async`).

**Run this exact Bash command** — do NOT call `mcp__teammate__ask`,
which is the whole point of this slash command. The MCP tool path
incurs:
- a deferred-schema `ToolSearch` round-trip on the first call per
  session (1–3 s),
- LLM extended-thinking time deciding to route to the tool (10–40 s
  on Opus with thinking enabled).

The CLI bypasses both:

```bash
teammate-mcp ask [--async] [--timeout N] <LABEL> "<QUESTION>"
```

Substitute `<LABEL>` and `<QUESTION>`; escape any literal `"` inside
the question as `\"`. If `teammate-mcp` is not on PATH, fall back to
the absolute venv path (`<repo>/.venv/bin/teammate-mcp`).

Print the command's stdout back to the user verbatim, then end the
turn. Do not summarise, do not add commentary, do not call any other
tool.

If the user did not supply both a label and a question, print:
`Usage: /ask [--async] <label> <question...>` and end the turn.
