# teammate-mcp

> Let **Claude Code** and **OpenAI Codex** panes talk to each other —
> by **label**, by **iTerm session name**, or by **session id**.
> N agents, M agents, mix-and-match. No daemon. No `.config` per project.

```
┌──────────── iTerm window ─────────────┐
│ claude  (left)        codex  (right)  │
│ ───────────────────   ─────────────── │
│ > implement quoter    > [teammate-mcp │
│   I'll ask Codex...     ASK ... what  │
│   ⏺ Codex answered:     is 2+2?]      │
│      4                  • 4           │
└───────────────────────────────────────┘
```

`teammate-mcp` is a tiny MCP server that gives every loaded CLI a
small toolbox:

- `ask(target, question, timeout)` — primary. `target` is a label, an
  iTerm session name, or a session-id prefix.
- `list_panes()` — every live pane + its label/name/id/job/cwd.
- `register_self(label)` — attach a label to the calling pane at runtime.
- `broadcast(message, targets=[...])` — push to multiple panes at once.
- `ask_codex` / `ask_claude` — legacy 1:1 shortcuts; still work when
  exactly one of each CLI is running.

The server uses the [iTerm2 Python API](https://iterm2.com/python-api/)
to push the prompt into the target pane and read the reply back via a
unique marker (`<<DONE_…>>`).

## Why?

Existing multi-agent harnesses fall into two camps:

1. **Heavyweight**: a daemon, per-project config files, opaque session
   state. Great until something breaks at 2 AM and you can't see why.
2. **Single-process**: one model orchestrating sub-agents internally,
   so the user only sees the final answer.

`teammate-mcp` aims for a third option: the two agents are visibly
running in your terminal *next to each other*, you can read both
transcripts in real time, and the only "infrastructure" is a few
hundred lines of Python that pushes text and reads screens.

## Verified bidirectional round trip

Captured live during development on macOS 14, iTerm 3.6.8,
Claude Code 2.1.119 + Opus 4.7, Codex 0.125.0:

```jsonl
{"event":"ask.enqueue","id":"…c5d085","from_":"claude","to":"codex","len":49}
{"event":"ask.send",   "id":"…c5d085","to":"codex","session_id":"7E39032F-…"}
{"event":"ask.complete","id":"…c5d085","answer_len":3}
```

The `ask.send` → `ask.complete` interval was **3.0 seconds** for a
prompt of "What is two plus two? Answer with the digit only" — the bulk
of which is Codex thinking time, not the bridge. Five consecutive runs
all closed the loop in 1.5 – 4.5 seconds.

Six independent timing reports captured in `tests/results/` are
included in the repo so you can audit the numbers yourself.

---

## Quick start

### 1. Install

```sh
git clone https://github.com/jonghklee/teammate-mcp.git
cd teammate-mcp
uv venv
uv pip install -e .
```

### 2a. One-shot Claude Code wiring (recommended)

```sh
./bin/install-claude
```

Idempotent. This single script:

1. Registers the MCP server with Claude Code (`claude mcp add teammate …`)
2. Symlinks every `commands/*.md` into `~/.claude/commands/` so the
   `/ask`, `/tmclaude`, `/tmcodex`, `/team-list`, `/team-register`
   slash commands work in every Claude Code session
3. Inserts the `templates/CLAUDE.md` natural-language routing block
   into `~/.claude/CLAUDE.md` between `<!-- TEAMMATE_MCP_START -->`
   markers (replaces on re-run, never duplicates; backs up first)
4. Appends `<repo>/.venv/bin` to your `~/.zshrc` PATH so `teammate-mcp`,
   `tmclaude`, `tmcodex` resolve without an absolute path

After this, in a *new* shell:

```
teammate-mcp version          # CLI on PATH
tmclaude                      # register THIS pane and start Claude Code
/ask <label> <question>       # inside Claude Code, fast cross-pane ask
```

Flags: `--dry-run` (preview), `--no-path` (skip the PATH change).

### 2b. Codex (manual)

```sh
codex mcp add teammate -- $PWD/.venv/bin/teammate-mcp serve
```

Codex has no slash commands; it picks up `templates/AGENTS.md` from
your project root automatically.

### 3. Open the panes

You have two options:

**Option A** — let `bin/team` open a fresh iTerm window for you:

```sh
./bin/team
```

**Option B** — use any iTerm window you already have open. Just run
`claude` in one pane and `codex` in another. teammate-mcp finds them
by process name; no labels needed.

### 4. (One-time) Hand the agents the operating rules

Drop `templates/AGENTS.md` into your project root. Both Claude Code
and Codex will pick it up automatically (it's the convention they
both follow). The file tells them how and when to call each other.

### 5. (Optional) Add per-pane labels for N:M setups

For more than one agent of either type, label each pane *before*
launching its CLI:

```sh
# pane 1
export TEAMMATE_LABEL=plan
claude

# pane 2
export TEAMMATE_LABEL=worker
codex

# pane 3
export TEAMMATE_LABEL=tester
codex --yolo
```

The MCP server auto-registers each pane to its label on startup.
Then from any pane:

```
ask("worker",  "implement foo()")
ask("tester",  "write tests for foo()")
ask("plan",    "review this design")    # from worker, asking back
```

You can also address a pane by the iTerm session name (`cmd+I`) or by
any prefix of its UUID — `ask("Worker A", …)` or `ask("7B5B0D11", …)`.

### 6. (Optional) Show the label in your status bar

```sh
./bin/install-statusline
```

Adds a `statusLine` block to `~/.claude/settings.json` and a precmd
hook to `~/.zshrc` that updates the iTerm tab title from
`$TEAMMATE_LABEL`. Both Claude (native statusLine) and Codex (tab
title) show the label visibly. Idempotent; backs up your existing
settings.

### 7. Try it

In the Claude pane:

```
Ask the worker pane what timezone library it prefers in Python.
```

You'll see Claude call `ask`, the question appear in the *worker*
pane, Codex respond there, and Claude relay the answer.

---

## Slash commands (Claude Code)

Two slash commands ship with the repo, in `commands/`. Drop them into
`~/.claude/commands/` (or `<project>/.claude/commands/`) to use them.

### `/ask <label> <question…>` — fast path

Routes the question to the target pane via the **CLI directly**,
bypassing the MCP tool. Use this when you already know the label.

```
/ask claude20 위 코드의 시간복잡도가 어떻게 돼?
/ask worker  pytest를 실행해줘
/ask codex1  Python에서 timezone-aware datetime 만드는 가장 좋은 방법?
```

Why this exists: the MCP tool path (`mcp__teammate__ask`) is correct
but slow when invoked from Claude Code — Anthropic defers MCP tool
schemas to a `ToolSearch` lookup (1–3 s extra) and Opus extended
thinking adds another 10–40 s deciding to route. `/ask` collapses
that to a single deterministic Bash call.

Empirical comparison on the same physical pane (claude20 → "ack"):

| Path                                 | Round trip |
|--------------------------------------|------------|
| Natural language → `mcp__teammate__ask` | 30–80 s   |
| `/ask claude20 …`                    | 3–6 s     |
| Plain CLI in shell                   | 2–4 s     |

### `/team-ask <label> <question…>` — MCP path

The original. Goes through `mcp__teammate__ask`. Use when you want
the tool-call to be visible in the transcript, or when the `ask`
needs to be part of a larger LLM-mediated workflow.

### Other slash commands

- `/team-register` — alias for `tmclaude` / `tmcodex`. Registers the
  current pane in the registry.
- `/team-list` — print all registered panes.

## Natural-language routing

You can also just say it:

```
claude20에게 안녕이라고 물어봐
codex1한테 README 검토해달라고 해
worker에게 빌드 다시 돌려달라고 시켜
```

The bundled `templates/CLAUDE.md` (drop into `~/.claude/CLAUDE.md`)
tells Claude to prefer the **Bash CLI** for asks where the user
already gave an explicit label, and to fall back to
`mcp__teammate__ask` only when the target is ambiguous (and a
`list_panes` call is needed first). This keeps natural-language asks
fast without sacrificing the MCP path's flexibility.

---

## How it works

```
┌──────────────────────────────────────────────────────┐
│  Claude pane              Codex pane                  │
│  ─────────────            ─────────────               │
│   user prompt              [teammate-mcp ASK …]       │
│        │ tool call              ▲                     │
│        ▼                        │ async_send_text     │
│  ┌──────────────┐               │                     │
│  │ teammate-mcp │  ─────────────┘                     │
│  │  (FastMCP)   │  ◄────── async_get_screen_contents  │
│  └──────────────┘                                     │
│        │                                              │
│        └─► returns extracted answer to Claude         │
└──────────────────────────────────────────────────────┘
```

For each `ask_codex` (or `ask_claude`) call:

1. Generate a unique marker, enqueue the message in the on-disk queue
   (`pending/` → `inflight/` atomic rename).
2. Locate the target pane:
   - prefer ``TEAMMATE_<UPPER>_SESSION_ID`` env override
   - otherwise enumerate all live processes (`ps`-style), find any
     `claude` or `codex` process, read its `TERM_SESSION_ID` env var,
     and match that against iTerm's session list. **This works through
     `tmux`, login shells, and pyenv wrappers** — anywhere the
     environment variable is inherited.
   - fall back to `jobName` / `commandLine` matching with cwd
     preference.
3. `async_send_text` the prompt + a request to terminate the reply
   with the marker.
4. Poll `async_get_screen_contents` for the marker. Because the prompt
   we typed contains the marker text (it gets echoed in the pane), the
   server requires the marker to appear **twice** before treating the
   reply as complete.
5. Slice the answer between the two marker occurrences, log
   `ask.complete`, return the answer to the caller.

### What "no config" actually means

There is exactly one thing to configure (once): the MCP registration
in step 2 above. After that, any iTerm window with claude+codex panes
just works — including windows that were already open before you
installed teammate-mcp.

You never write a `.teammate.toml`, you never `teammate start`, you
never have to remember which session id is which.

## Testing

```sh
uv pip install -e ".[dev]"
pytest                              # 18 unit + integration tests
python scripts/auto_demo.py         # full end-to-end demo (spawns iTerm)
```

The unit tests cover the queue, ANSI/marker handling, server module
import, and the iTerm session-discovery logic with mocks. The
end-to-end demo opens a real iTerm window and exercises a Claude →
Codex → Claude round trip; it requires both CLIs to be logged in and
will incur their normal API charges.

Per-run timing reports are written to `tests/results/*.jsonl`. The
ones already committed to the repo are real, not synthetic.

## Troubleshooting

**"iTerm Python API is not enabled"** — Settings → General → Magic →
"Enable Python API" ✓. The first time `teammate-mcp` connects, iTerm
also prompts for permission; click *Allow*.

**"asyncio.run() cannot be called from a running event loop"** — you're
on a teammate-mcp older than 0.1.0. Pull `main`; the tools are now
declared `async`.

**"Tool returned an answer that's just my own prompt echo"** — the
prompt-target pane is running the wrong CLI (e.g., the lookup picked a
sibling pane that had the same process running). Pin the pane
explicitly:

```sh
export TEAMMATE_CLAUDE_SESSION_ID=<unique id from iTerm>
export TEAMMATE_CODEX_SESSION_ID=<unique id from iTerm>
```

(You can read each pane's `unique id` from
`Window menu → Window Settings → Identifier`, or via AppleScript.)

**"Marker not detected within timeout"** — the agent on the other end
forgot to emit `<<DONE_…>>`. Add an explicit reminder in your
`AGENTS.md`. The bundled template already includes this.

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgments

This project crystallised from conversations on top of public research
into how Claude Code and Codex are being run in 2026:

- Anthropic's *Plan-Generate-Verify* and *Initializer + Coding Agent*
  harness papers (Rajasekaran 2026-03; Justin Young 2025-11).
- IndyDevDan's [`claude-code-hooks-mastery`](https://github.com/disler/claude-code-hooks-mastery)
  for the observability patterns.
- OthmanAdi's [`planning-with-files`](https://github.com/OthmanAdi/planning-with-files)
  for the "structured files bridge sessions, not chat history" idea.
- Boris Cherny's "verification loop" rule from his
  *How I use Claude Code* thread.
- Geoffrey Huntley's [Ralph Wiggum](https://ghuntley.com/ralph/)
  loop for the "fresh context per turn" intuition.

The implementation owes its iTerm Python API patterns to the iTerm2
docs at <https://iterm2.com/python-api/>.

---

## 한국어 요약

CCB 같은 사전 설정 없이 **claude / codex가 서로에게 질문**할 수 있게
해주는 작은 MCP 서버입니다.

- iTerm 두 페인에 그냥 `claude`와 `codex`를 띄우기만 하면 됩니다.
  라벨도, config도, daemon도 없습니다.
- iTerm Python API로 상대 페인을 자동 탐지(실행 프로세스 + 환경변수
  `TERM_SESSION_ID` 매칭)합니다 — `tmux` 안에서 띄워도 작동합니다.
- 메시지는 push, 응답은 polling으로 받고, 모든 round trip은
  `~/.teammate-mcp/logs/<날짜>.jsonl`에 기록됩니다.
- 실측 round-trip 시간: **2 + 2 = 4 질문 기준 send → complete 3.0초**
  (대부분 Codex thinking 시간).

설치는 위 영문 Quick start 1~3단계, 사용법은 그냥 평소처럼 Claude에게
"Codex에게 물어봐"라고 시키면 됩니다.
