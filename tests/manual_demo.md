# Manual Bidirectional Demo

This is the runbook for proving Claude ↔ Codex actually exchange messages
through `teammate-mcp`. It can't live in the automated suite because it
needs an interactive desktop session and live API providers.

## Prerequisites (one-time)

- iTerm2 ≥ 3.5
- iTerm Python API enabled  
  *Settings → General → Magic → "Enable Python API"* ✓
- `claude` CLI logged in (Claude Code 2.x)
- `codex` CLI logged in (Codex 0.12.x)
- `teammate-mcp` installed and registered on **both** CLIs:
  ```sh
  uv pip install -e ~/programming/teammate-mcp           # editable install
  claude mcp add teammate -s user -- ~/programming/teammate-mcp/.venv/bin/teammate-mcp serve
  codex  mcp add teammate            -- ~/programming/teammate-mcp/.venv/bin/teammate-mcp serve
  ```

## Open the panes

Run from the project root:

```sh
cd ~/programming/teammate-mcp
./bin/team
```

That opens a fresh iTerm window split into two zsh sessions, the left one
running `claude` and the right one running `codex`. (You can also do this
manually in any window — `teammate-mcp` finds the CLIs by process, not by
pane title.)

> First time only: iTerm will pop a confirmation dialog asking whether to
> let `teammate-mcp` script the iTerm Python API. Click **Allow**.

## Hand the agents their operating rules

Paste the contents of `templates/AGENTS.md` into both panes. (You can
either drop a copy at the project root so both agents auto-load it, or
just `cat templates/AGENTS.md | pbcopy` and paste into each.)

## Scenario A — Claude asks Codex (single hop)

In the **Claude** pane, type:

```
Use mcp__teammate__ask_codex to ask Codex: "What's 2 + 2?"
Wait for the reply, then tell me the answer.
```

What you should see:

1. Codex pane prints `[teammate-mcp ASK <id> from=claude]` followed by the
   question and the marker request line.
2. Codex composes a short answer ending with `<<DONE_<id>>>`.
3. Claude's tool call returns; Claude paraphrases the answer to you.

Approximate timing on a warm M-series Mac (from `~/.teammate-mcp/logs/`):

| phase | typical |
|---|---|
| `ask.enqueue` | < 1 ms |
| `ask.send` (push to Codex pane) | 30 – 80 ms |
| Codex thinking + writing answer + marker | model-dependent, 2 – 8 s |
| `ask.complete` (marker detected, screen captured) | within poll interval (≤ 1.5 s) |

## Scenario B — Codex asks Claude (reverse direction)

In the **Codex** pane:

```
Use mcp__teammate__ask_claude to ask Claude:
"Briefly: when should I prefer DSPy over plain prompt engineering?"
Then summarise its reply.
```

Same flow, opposite direction. This is the path that guarantees the system
is genuinely bidirectional rather than wired one-way.

## Scenario C — Two-hop chain (depth 2)

In Claude:

```
1. Use ask_codex to ask Codex which Rust crate it prefers for PDF parsing.
2. Then use ask_codex again with a follow-up that builds on its answer.
3. Report Codex's final position.
```

Watch the chain unfold across both panes. AGENTS.md caps depth at 3 to
prevent runaway loops.

## Scenario D — Concurrent asks (queue ordering)

In Claude:

```
Fire three ask_codex calls in parallel asking Codex to count "one",
"two", "three" respectively. Show me the order of replies.
```

Replies should arrive in the order their `<<DONE_…>>` markers were
emitted. Inspect `~/.teammate-mcp/logs/<today>.jsonl` to confirm the
`ask.enqueue` and `ask.complete` events line up.

## Scenario E — Timeout recovery

In Claude:

```
Use ask_codex with timeout=5 to ask Codex something it can't answer in
5 seconds (e.g. "Spend 30 seconds quietly thinking, then say hi.")
What does the tool return?
```

Expected: the tool returns `TIMEOUT: ...` and the queue's `failed/`
directory has the message recorded. Codex's later (late) reply lands in
its own pane harmlessly.

## What "verified" means

The demo passes if you observe, on screen and in `~/.teammate-mcp/logs/`:

- ✅ A → B push happens within ~100 ms (the `ask.send` event).
- ✅ B → A pull captures the answer once the marker appears (the
  `ask.complete` event lines up with the `<<DONE_…>>` line on screen).
- ✅ Reverse direction works symmetrically.
- ✅ Timeout returns gracefully; nothing wedges.

If any of those fail, see Troubleshooting in the project README.
