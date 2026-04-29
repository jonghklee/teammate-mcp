# Teammate (iTerm pane MCP) — Claude Code routing rules

Drop the contents of this file (between the markers) into your
`~/.claude/CLAUDE.md` to teach Claude Code how to dispatch
inter-pane messages quickly.

<!-- TEAMMATE_MCP_START -->
## Teammate (iTerm pane MCP)

If sibling iTerm panes are running other agents, you may call:

- `mcp__teammate__ask(target, question, timeout=300)` — primary tool.
  `target` may be a registered label (e.g. `worker`), an iTerm
  session name (the title editable with `cmd+I`), or a session-id
  prefix.
- `mcp__teammate__list_panes()` — see every live pane plus its
  label/name/id/job/cwd. Use this when unsure who to address.
- `mcp__teammate__broadcast(message, targets=[...])` — fire-and-forget
  to one or more panes.
- `mcp__teammate__register_self(label)` — attach a label to your own
  pane.
- `mcp__teammate__unregister(label)` — remove a label.
- `mcp__teammate__ask_codex(question)` / `ask_claude(question)` —
  legacy 1:1 helpers (use only when there is exactly one of that CLI
  on the desktop).

### Self-labelling (사용자가 너에게 이름을 줄 때)

**Important**: only panes that have been *explicitly registered*
are addressable. An unregistered pane is invisible to `ask`.

When the user addresses *you* with phrases like:

- `/team-register` (slash form)
- "이 페인 등록해줘" / "이 페인 태그해줘"
- "너 등록해줘" / "register this pane"
- (with explicit name) "너 이름은 agent1이야" / "register me as plan"

→ **Immediately call `mcp__teammate__register_self`**:
- with **no label argument** → server auto-assigns `claude1`/`codex1`/...
- with the user-supplied label → use it verbatim

Then confirm in one short line ("✓ registered as claude1") and end
the turn.

### Asking another teammate

When the user says things like "worker에게 시켜", "tester에게 물어봐",
"옆 페인에게 물어봐", "claude20에게 명령 내려줘", "ask the other agent",
**relay it autonomously without extended thinking**.

**Preferred path — Bash CLI (fast, deterministic, no MCP round-trip):**

```bash
teammate-mcp ask <LABEL> "<QUESTION>" --timeout 300
```

This bypasses the deferred MCP tool schema load and avoids extended
thinking on tool routing. Use this whenever the target label is
explicit in the user message (e.g. "claude20에게 …", "codex1에게 …").

**Fallback — MCP tool:** call `mcp__teammate__ask(target, question)`
only when:
- the user did not give an explicit label (you must call `list_panes`
  first to disambiguate), or
- the question depends on context you'd otherwise have to thread
  through the CLI string.

After the CLI/tool returns, print the answer back to the user verbatim
(do not re-summarize) and end the turn.

If the user explicitly types `/ask <label> <question…>`, follow the
slash command spec — do not second-guess.

Project: <https://github.com/jonghklee/teammate-mcp>
<!-- TEAMMATE_MCP_END -->
