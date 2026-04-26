---
description: "Register THIS pane in teammate-mcp under an auto-assigned label (claude1, codex1, codex2, ...)."
---

Call `mcp__teammate__register_self` with no `label` argument (empty
string). If the user explicitly supplied a label in the slash
arguments, pass that as `label` instead.

The server returns the chosen label. Reply with one short line:

```
✓ registered as <label>
```

Then end the turn. Do not call any other tool.
