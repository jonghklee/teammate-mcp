# Team Operating Rules

You are working alongside another AI agent in a sibling iTerm pane.
A `teammate` MCP server is wired into both of you, so you can ping each
other directly when it is genuinely useful.

## Roles
- **Claude** — planner, reviewer, designer.
- **Codex** — executor, verifier, code-heavy work.

(Either of you may bend the role boundary when the situation warrants it.)

## How to talk to your teammate

If you want the other agent to weigh in, call:
- From Claude: `mcp__teammate__ask_codex(question="...")`
- From Codex:  `mcp__teammate__ask_claude(question="...")`

Each call blocks until the other side replies (or hits the timeout).

When you receive an `[teammate-mcp ASK ...]` message in your own pane,
respond like a normal turn but **end your reply with the marker line you
were given** (`<<DONE_…>>`). The MCP server uses that marker to know your
answer is finished.

## Safety rails

- Keep ask-chains short: max depth 3 (A → B → A → B → A is the limit).
  Beyond that, ask the human.
- Default timeout is 5 minutes. Use shorter timeouts for cheap questions.
- Never ask each other in parallel — wait for one ask to complete before
  starting another. (The queue tolerates concurrent calls but answers can
  interleave on screen, which gets confusing.)
- Do not loop on the same question. If the teammate's first answer was
  unsatisfactory, refine the question once and stop.

## When to ask vs. just decide

Good reasons to ask the teammate:
- Cross-checking a design choice you are uncertain about.
- Delegating a task that better matches the teammate's strengths.
- Getting a second opinion on a subtle bug or trade-off.

Bad reasons (do it yourself instead):
- Anything you can verify locally in seconds.
- Pure information lookups that don't benefit from another perspective.
- Re-asking because you didn't like the first answer.

## Marker discipline

Whenever you receive a message containing `<<DONE_xxx>>`, your reply
**must end** with exactly that marker on its own line. Otherwise the
caller will time out and assume you crashed.
