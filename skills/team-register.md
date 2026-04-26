---
name: team-register
description: "Register THIS iTerm pane in teammate-mcp under an auto-assigned label (claude1, codex1, codex2, ...). Trigger this on /tmclaude, /tmcodex, /tm, /register, /team-register, or when the user says to tag/register this pane in Korean or English."
---

# team-register

The user wants this pane to be addressable by sibling panes through
the teammate-mcp bridge. Run the registration once.

## Action — exactly these steps, nothing else

1. Call `mcp__teammate__register_self` with **no arguments** (label = "").
   The server picks the next free `claude1` / `codex1` / `codex2` / ...
   based on the calling pane's job. The server returns the chosen label.

2. Reply with **one short line** containing the returned label, e.g.:

   ```
   ✓ registered as claude1
   ```

3. **End the turn. Do not call any other tool.**

If the user supplied an explicit label (e.g. "register me as worker"),
pass it as the `label` argument; otherwise leave it empty.

Re-running on an already-registered pane is safe — the existing label
is reused.

## Trigger phrases

The user may invoke this skill via any of:

- `/tmclaude` (when running Claude — registers this Claude pane)
- `/tmcodex`  (when running Codex — registers this Codex pane)
- `/tm`
- `/register`
- `/team-register`
- "이 페인 등록해줘"
- "이 페인 태그해줘"
- "register this pane"
- "tag this pane"
- "register me as <name>"  → pass <name> as the label

Treat all of the above the same: a single `register_self` call followed
by a one-line confirmation.
