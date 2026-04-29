---
description: "Drain this pane's teammate-mcp inbox now. Runs `teammate-mcp drain` via Bash; the hook auto-fires on submit so this also works as a wake signal injected by the watchdog. Usage: /drain"
---

Run this exact Bash command and print its stdout verbatim:

```bash
teammate-mcp drain
```

(or the absolute venv path if not on PATH).

Do not call any MCP tool. Do not summarise. End the turn.

Why: the UserPromptSubmit hook drains the inbox automatically on every
prompt — invoking `/drain` simply triggers a prompt submit, which is
useful when:
- you want to fetch pending mail without typing a real question,
- the watchdog daemon injected `/drain` as a wake signal because mail
  arrived while this pane was idle,
- you want to inspect inbox content explicitly.
