---
description: "Register THIS Claude pane under an auto-assigned label (claude1, claude2, ...)."
---

Call `mcp__teammate__register_self` with no `label` argument. The
server auto-assigns `claude1` / `claude2` / ... based on this pane's
job and returns the chosen label.

Reply with one short line: `✓ registered as <label>`. Then end the
turn. Do not call any other tool.
