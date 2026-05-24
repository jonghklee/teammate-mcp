# teammate-mcp Test Spec

## Goals

This spec covers the stability work for teammate-mcp:

- Prevent Claude Bash-generated orphan polling loops from accumulating.
- Ensure spawned Codex panes run Codex and spawned Claude panes run Claude.
- Ensure ask delivery does not leave messages stuck in the receiver compose box.
- Keep registry, daemon, watchdog, hook, CLI, and iTerm helper behavior covered by automated tests.
- Define live wordchain acceptance tests for Claude-only, Codex-only, mixed, and three-pane scenarios.

## Automated Unit Coverage

Run:

```bash
.venv/bin/pytest -q
```

Coverage map:

- `tests/test_block_infinite_poll.py`
  - Blocks the exact incident pattern: `until [ now -gt now+N ] ... sleep`.
  - Blocks unbounded `while true` / `while :` teammate-mcp polling loops.
  - Allows bounded loops and single-shot commands.
  - Verifies the Claude PreToolUse hook exits `2` with a clear block message.

- `tests/test_cli_spawn.py`
  - Verifies `teammate-mcp spawn <label> codex --yolo` builds a Codex shell payload, not Claude.
  - Verifies Claude spawn builds a Claude shell payload, not Codex.
  - Decodes the base64 shell payload embedded in the AppleScript so the actual executed command is checked.

- `tests/test_server_labels.py`
  - Verifies job name wins over stale iTerm session title when auto-labeling.
  - Prevents `job=codex, session_name="Claude Code"` from becoming `claudeN`.
  - Keeps fallback classification by session name for uninformative jobs such as `Python` or `zsh`.

- `tests/test_server_ask_safety.py`
  - Verifies control bytes are stripped before prompt injection.
  - Verifies Claude footer text does not false-positive as a picker.
  - Verifies real permission/picker menus are detected and injection is skipped.
  - Verifies plain-text numbered options are not treated as TUI pickers.
  - Verifies old busy markers in scrollback do not hide a message stuck in compose.
  - Verifies current busy tails are treated as active processing, not stuck compose.
  - Verifies mailbox-only ask writes the receiver inbox without keystroke injection.

- `tests/test_cli_ask.py`
  - Verifies `teammate-mcp ask --mailbox-only <label> ...` is parsed as an option, not as the target label.
  - Verifies `TEAMMATE_MCP_MAILBOX_ONLY=1` forces mailbox-only delivery for scripted test replies.

- `tests/test_cli_replies.py`
  - Verifies `teammate-mcp next-reply <mailbox-label> <sender>...` reads `inbox/` and `processed/`.
  - Verifies replies are matched by `from_` or by `body` prefix such as `round-codex-1:서울`.
  - Verifies the oldest `created_at` match is returned first.
  - Verifies `--consume` deletes the matched JSON so scripts can safely loop.

- `tests/test_iterm.py`
  - Verifies command-line matching avoids helper-module false positives.
  - Verifies real Claude/Codex CLI invocations still match.
  - Verifies injected body and standalone carriage return are sent as separate AppleScript writes.
  - Verifies raw send AppleScript has balanced repeat blocks.

- `tests/test_watcher.py`
  - Verifies watchdog health states for missing, healthy, and stale heartbeat.
  - Verifies `ensure_watchdog_running()` does not spawn a second watchdog when heartbeat is fresh.
  - Verifies a stale watchdog starts exactly one detached replacement.
  - Verifies empty and typed Claude compose screens are distinguished.

- `tests/test_daemon.py`
  - Verifies daemon-side register dedupes session IDs.
  - Verifies daemon lookup by label and session prefix.
  - Verifies daemon health reports process uptime and label count.

- Existing tests:
  - `tests/test_registry.py`: registry persistence and stale PID policy.
  - `tests/test_queue.py`: queue state transitions.
  - `tests/test_e2e.py`: live iTerm session enumeration and Claude/Codex lookup when iTerm is running.
  - `tests/test_smoke.py`: import and package smoke checks.

## Live Wordchain Acceptance Matrix

These tests require real iTerm panes and real Claude/Codex CLIs. They are intentionally not part of the default pytest suite.

Before each scenario:

```bash
teammate-mcp prune
teammate-mcp watch --ensure
teammate-mcp watch --health
```

Record leak baseline:

```bash
ps -eo pid,ppid,etime,rss,command | rg 'claude|codex|teammate-mcp|agent-deck' > /tmp/tmm-before.txt
```

Run each scenario for at least 10 rounds:

```bash
LABELS="a b" ./scripts/wordchain_test.sh "사과" 10
LABELS="c1 c2" ./scripts/wordchain_test.sh "사과" 10
LABELS="claude1 codex1" ./scripts/wordchain_test.sh "사과" 10
LABELS="a b c" ./scripts/wordchain_test.sh "사과" 10
```

Scenario meanings:

- Claude-only 2 panes: both labels backed by `tmclaude`.
- Codex-only 2 panes: both labels backed by `tmcodex`.
- Mixed 2 panes: one `tmclaude`, one `tmcodex`.
- Three panes: any stable mix; recommended `tmclaude`, `tmcodex`, `tmclaude`.

Latest local acceptance run:

- Claude-only 2 panes: `LABELS="claude1 claude2" ... "사과" 10` exited `0`.
- Mixed 2 panes: `LABELS="claude1 codex2" ... "사과" 10` exited `0`.
- Codex-only 2 panes: `LABELS="codex2 codex3" ... "사과" 10` exited `0`.
- Three panes: `LABELS="claude1 codex2 claude2" ... "사과" 10` exited `0`.

The script instructs participants to reply with
`teammate-mcp ask --mailbox-only <caller> "<단어>"` so the test runner can
poll the caller mailbox without the reply being injected into the runner's
own Codex/Claude compose UI.

For custom live orchestration that closes panes as replies arrive, use
`teammate-mcp next-reply --consume <caller> <label1> <label2> ...` instead of
hand-written shell globs. It emits the oldest matching reply as JSON and
matches both `from_` and `label:` body prefixes.

After each scenario:

```bash
ps -eo pid,ppid,etime,rss,command | rg 'claude|codex|teammate-mcp|agent-deck' > /tmp/tmm-after.txt
diff -u /tmp/tmm-before.txt /tmp/tmm-after.txt || true
```

Pass criteria:

- The wordchain script exits `0`.
- At least 10 rounds complete.
- `teammate-mcp watch --health` reports one healthy watchdog.
- No new PPID=1 `teammate-mcp drain`, `teammate-mcp watch`, or shell polling loop remains.
- No `until ... date +%s ... date +%s + N` process exists.
- RSS growth is bounded to active Claude/Codex work, not unbounded teammate-mcp helper accumulation.

## Incident Regression Commands

The Bash guard must block the historical runaway loop:

```bash
teammate-mcp-hook-block-infinite-poll <<'JSON'
{"tool_name":"Bash","tool_input":{"command":"until [ \"$(date +%s)\" -gt \"$(($(date +%s) + 25))\" ] || teammate-mcp drain 2>&1 | grep -q \"ASK\"; do sleep 2; done"}}
JSON
```

Expected:

- Exit code `2`.
- Stderr starts with `Blocked teammate-mcp polling loop`.

The guard must allow single-shot drain:

```bash
teammate-mcp-hook-block-infinite-poll <<'JSON'
{"tool_name":"Bash","tool_input":{"command":"teammate-mcp drain agent1"}}
JSON
```

Expected exit code: `0`.
