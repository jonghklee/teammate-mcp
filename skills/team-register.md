---
name: team-register
description: Register the current iTerm pane with teammate-mcp under an auto-assigned label (claude1, codex1, codex2, ...). Use when the user runs /team-register, says "이 페인 등록해줘", "tag this pane", or wants to make this pane addressable from sibling panes.
---

# team-register

Register *this* iTerm pane with the `teammate-mcp` server so sibling
panes can address it by label.

## What to do

1. Call `mcp__teammate__register_self()` with **no label argument** (or
   an empty string). The server will auto-assign the next free
   `{job}{n}` slot — `claude1` if you are Claude, `codex1`/`codex2`/...
   if you are Codex, `agentN` otherwise. The server returns the label
   it chose.

2. Echo the returned label back to the user in one short line, e.g.:

   ```
   ✓ registered as claude1
   ```

3. End the turn. Do not call any other tool.

## Notes

- The server also writes an iTerm tab-title escape sequence so the
  chosen label shows up on the pane's tab/title bar automatically.
- If the user provides an explicit label (e.g. "register me as plan"),
  pass it as the `label` argument instead of an empty string.
- Re-running this on an already-registered pane is safe: the existing
  label is reused, not duplicated.

## Trigger phrases

- `/team-register` (slash command form)
- "이 페인 등록해줘" / "이 페인 태그해줘"
- "register this pane" / "tag this pane"
- "register me as <name>" → pass the name as the label
