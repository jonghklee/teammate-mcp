# teammate-mcp

> Let **Claude Code** and **OpenAI Codex** ask each other questions
> through your iTerm panes. No daemon. No `.config` you have to
> hand-edit per project. Just open two panes and they can talk.

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

`teammate-mcp` is a tiny MCP server that exposes two tools to whichever
CLI loads it:

- `mcp__teammate__ask_codex(question, timeout)` — call from Claude
- `mcp__teammate__ask_claude(question, timeout)` — call from Codex

The server uses the [iTerm2 Python API](https://iterm2.com/python-api/)
to push the question into the *other* pane and read the reply back. The
target pane is detected automatically by the running process — you don't
label tabs, you don't pre-configure anything per project.

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

### 2. Register the server with both CLIs

```sh
# Claude Code
claude mcp add teammate -s user -- $PWD/.venv/bin/teammate-mcp serve

# Codex
codex  mcp add teammate           -- $PWD/.venv/bin/teammate-mcp serve
```

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

### 5. Try it

In the Claude pane:

```
Ask Codex what timezone library it prefers in Python and tell me what
it said.
```

You'll see Claude call `mcp__teammate__ask_codex`, the question
appear in the right-hand pane, Codex respond, and Claude relay the
answer.

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
