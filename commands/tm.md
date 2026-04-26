---
description: "Register THIS pane in teammate-mcp under an auto-assigned label (claude1, codex1, codex2, ...)."
---

Call `mcp__teammate__register_self` with no `label` argument (empty
string). The server picks the next free `claude1` / `codex1` / ...
slot based on this pane's job and returns the chosen label.

Reply with one short line containing the returned label, e.g.:

```
✓ registered as claude1
```

Then end the turn. Do not call any other tool.
