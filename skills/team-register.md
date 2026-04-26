---
name: team-register
description: "Register THIS iTerm pane in teammate-mcp via the CLI (claude1, codex1, codex2, ...). Trigger this on /tmclaude, /tmcodex, /tm, /register, /team-register, or when the user says to tag/register this pane in Korean or English."
---

# team-register (CLI-based)

The user wants this pane to be addressable by sibling panes through
the teammate-mcp bridge. Run the registration once via CLI — no MCP
round-trip required.

## Action — exactly these steps

1. Run this bash command and capture stdout:

   ```bash
   /Users/siheom-yong/programming/teammate-mcp/.venv/bin/teammate-mcp register-pane
   ```

   If the user supplied an explicit label (e.g. "register me as worker"),
   append it as the first argument:

   ```bash
   ... register-pane worker
   ```

2. Print the CLI's `✓ registered as <label>` line back to the user.

3. **End the turn. Do not call any MCP tool.**

Re-running on an already-registered pane is safe — the existing label
is reused.

## Trigger phrases

- `/tmclaude` / `/tmcodex` / `/tm` / `/register` / `/team-register`
- "이 페인 등록해줘" / "이 페인 태그해줘"
- "register this pane" / "tag this pane"
- "register me as <name>" → pass <name> as the first CLI argument
