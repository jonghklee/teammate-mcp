---
description: "Ask another registered pane via the teammate MCP tool. Default ASYNC/mailbox. Usage: /ask <label> <question...>"
---

Parse the slash arguments. The first non-flag token is the target
label (e.g. `claude1`, `codex1`, `worker`). Everything after it is the
question, taken verbatim.

**As of v0.8.0 the default is ASYNC** (mailbox / file-only delivery).
The caller is never blocked, and the target's compose box / interactive
bash / permission prompts are never corrupted by injected keystrokes.

Supported flags (anywhere in the args):
- `--async` / `--no-wait`: explicit async (default; included for clarity).
- `--timeout N`: accepted for compatibility and passed to the MCP tool.
- `--wait`: deprecated; ignore it. Do not use sync/Bash dispatch.

Call `mcp__teammate__ask` with:
- `target` = the parsed label
- `question` = the rest of the arguments, verbatim
- `timeout` = parsed `--timeout` value if present, otherwise 300

Do not write or simulate XML/tool tags such as `<invoke>`. Do not use
Bash for teammate dispatch. The Bash path is intentionally avoided
because Claude Code sessions can leak malformed tool-call text into the
conversation instead of executing the command.

Print the MCP tool's returned string back to the user verbatim, then end
the turn. Do not summarise, do not add commentary, do not call any other
tool.

If the user did not supply both a label and a question, print:
`Usage: /ask [--async] <label> <question...>` and end the turn.
