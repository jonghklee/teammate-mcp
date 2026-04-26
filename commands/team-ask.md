---
description: "Ask another registered pane a question and return its answer. Usage: /team-ask <label> <question...>"
---

Parse the slash arguments: the first whitespace-separated token is
the target label (e.g. `codex1`). Everything after it is the question.

Call `mcp__teammate__ask` with:
- `target` = the parsed label
- `question` = the rest of the arguments
- `timeout` = 300

Print the returned answer back to the user as-is, then end the turn.
