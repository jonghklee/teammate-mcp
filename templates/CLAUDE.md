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

**Trigger phrases — relay autonomously without extended thinking:**

The user is asking you to forward something to another pane whenever
they use any of these patterns. Treat the list as illustrative, not
exhaustive — match the *intent*, not the exact words.

Korean (가장 흔한 패턴들):
- 명령: "OO에게 명령 내려줘", "OO한테 명령 보내", "OO 시켜"
- 전달: "OO에게 전달해줘", "OO에게 전해줘", "OO한테 넘겨"
- 대화: "OO랑 대화해줘", "OO랑 이야기해", "OO와 얘기해봐"
- 질문: "OO에게 질문해줘", "OO한테 물어봐", "OO에게 ~~인지 물어봐"
- 부탁/지시: "OO한테 ~~ 부탁해", "OO에게 ~~ 시켜줘", "OO에게 ~~ 해달라고 해"
- 호출/응답확인: "OO 호출", "OO 거기 있어?", "OO ping"
- 의견: "OO 의견 들어봐", "OO 생각 물어봐", "OO한테 컨설트해"
- 보고/알림: "OO한테 알려줘", "OO에게 보고해", "OO한테 공유해"
- 발화 위임: "OO에게 ~~라고 말해줘", "OO한테 ~~라고 전해"
- broadcast 변형: "다들에게 ~~", "팀원 전체에게 ~~", "모두에게 알려"

English:
- "ask OO …", "tell OO …", "send OO …", "relay this to OO"
- "have OO do …", "check with OO", "consult OO"
- "ping OO", "ask the other agent", "ask the worker pane"
- "broadcast to all panes" → use `mcp__teammate__broadcast`

**Disambiguation — DO NOT trigger when:**
- "OO에 **대해** 알려줘" / "tell me about OO" → user wants info *about* OO,
  not to message it. Answer directly.
- "OO 페인에서 ~~" / "in OO's pane …" → may be locative description only.
  Trigger only when the verb is ask/send/relay/명령/물어 family.
- User refers to their own pane ("이 페인", "여기", "this pane") — that's
  the self-labelling case (register / whoami); do not call `ask`.

**When in doubt, prefer asking the user one short clarifying question
over silent guessing.** But for clear cases above, dispatch immediately.

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

### Receiving an async ASK (mailbox / email mode)

When you see an injected message that looks like::

    [teammate-mcp ASK <job_id> from=<sender> mode=async]
    <body>

    Reply when you can by calling:
    `teammate-mcp ask <sender> "<your reply>" --no-wait`

You handle it the same way you'd handle a normal user prompt, then
**send the reply back via a reverse async ask** so the original sender
isn't blocked. Concretely:

1. Compose your answer.
2. Call `mcp__teammate__ask(target=<sender>, question=<answer>, wait=False)`,
   or run `teammate-mcp ask <sender> "<answer>" --no-wait` via Bash.
3. Optionally call `mcp__teammate__mark_processed(job_id=<job_id>, reply=<answer>)`
   to move the message from the inbox/ to processed/ on your mailbox.

Never wait for the receiver of your reply to acknowledge — that would
re-introduce the very synchronous coupling the async mode is designed
to eliminate.

### Draining your inbox

If the user asks you to "check the inbox", "drain pending mail", or
"check for messages", call `mcp__teammate__inbox()` (no args — it
defaults to your own label). For each entry, decide whether to reply
(usually yes) and follow the receiving-async-ASK flow above.

Project: <https://github.com/jonghklee/teammate-mcp>
<!-- TEAMMATE_MCP_END -->
