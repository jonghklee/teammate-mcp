# Team Operating Rules

You are working alongside other AI agents in sibling iTerm panes. A
`teammate-mcp` server is wired in so you can address each pane by its
**label**, by its **iTerm session name**, or by its **session id
prefix** — whichever is most convenient.

## Reserved-keyword trigger — "등록" / "register"

If the user's *entire* message is one of these one-word commands
(possibly with surrounding whitespace or punctuation), treat it as
register-this-pane and **run the Bash CLI directly** — do NOT call
`mcp__teammate__register_self`, do NOT ask what they mean, do NOT do
anything else this turn:

- `등록` / `등록해` / `등록해줘` / `등록 진행` / `등록좀` / `등록 좀`
- `register` / `register me` / `tag me` / `tag this pane`
- `whoami 등록` / `이거 등록` / `이 페인 등록`

→ Mapping (rigid):

```bash
teammate-mcp register-pane
```

(or, if not on PATH: `<repo>/.venv/bin/teammate-mcp register-pane`)

Print the command's stdout verbatim and end the turn. The CLI is
pure, fast, and bypasses both the deferred-MCP-tool ToolSearch and
extended thinking.

If the user gave an explicit label ("register me as worker", "너 이름은
worker야"), pass it via env:

```bash
TEAMMATE_LABEL=<label> teammate-mcp register-pane
```


## How to address a teammate

```
mcp__teammate__ask(target=<label or name or id>, question="…")
mcp__teammate__list_panes()           # see who is registered
mcp__teammate__broadcast(message="…", targets=["worker", "tester"])
```

`target` resolution order:
1. **label** registered via `TEAMMATE_LABEL` env or `register_self`,
2. **iTerm session name** (the title editable with `cmd+I`,
   case-insensitive exact match),
3. **session_id prefix** (≥ 6 chars; UUID prefix or suffix).

The legacy 1:1 helpers `ask_codex(question)` and `ask_claude(question)`
still work — they just look up the *only* codex/claude pane on the
desktop. Use `ask(target=…)` whenever there's more than one of either.

## Roles (suggestion, not enforcement)

A common labelling for a 3-pane setup:

| Pane label | CLI    | Job |
|------------|--------|-----|
| `plan`     | claude | planner / reviewer |
| `worker`   | codex  | primary executor |
| `tester`   | codex  | tests + verification |

…but you can pick anything. The registry is just a label → session_id map.

## When to talk to a teammate

Good reasons:
- delegating to whichever teammate's role best matches the subtask,
- cross-checking a tricky design decision,
- parallelising work (`ask("worker", …)` and `ask("tester", …)` in
  separate calls).

Bad reasons:
- anything you can verify locally in seconds,
- pure information lookups,
- re-asking because you didn't like the first answer.

## Safety rails

- ask-chain depth ≤ 3 (A → B → A → B → A is the limit).
- 5-minute default timeout. Use shorter for cheap questions, longer
  for heavy thinking.
- Don't fire a *second* ask to the same target while the first is
  still in flight; the queue tolerates it but interleaved replies are
  confusing on screen.

## Marker discipline (mandatory)

When you receive a message containing `<<DONE_…>>`, your reply must
end with **exactly** that marker line on its own. The caller polls
for the marker to know your answer is finished.
