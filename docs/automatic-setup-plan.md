# Automatic setup and message-driven wake

Approved scope: 2026-09-08 user requested completion of automatic registration,
reliable general use, and removal of the repeated dot prompts.

## Design

The registry still provides stable return addresses. Registration becomes an
internal step, not a user command: iTerm identity at server start; Codex's
authoritative `_meta.threadId` at first tool invocation (environment only when
request metadata is absent). Request identity must never leak across concurrent
calls through process environment mutation. New Codex mailboxes enable idle
delivery when the matching local app-server thread accepts direct input.

Claude keeps its existing supported terminal delivery, but there must be no
synthetic dot prompt. Direct injection claims a time-bounded delivery lease.
Watchdog only handles an unclaimed pending envelope and injects its actual body
with the original job ID, preserving history and reply correlation. Recheck
pending status and typing immediately before submitting.

## Four implementation/review cycles

1. `session_identity.py`, server dispatch: tests for request metadata precedence,
   automatic registration, concurrent caller isolation, idempotent reconnect.
2. Server envelope and watcher: test that in-flight direct delivery is skipped,
   stale candidate files are ignored, actual message replaces dot, failed
   injection remains inspectable and recoverable.
3. Registration/tool surface and operating docs: report ready/manual/unavailable
   truthfully; list pane and mailbox addresses; recover known failed deliveries.
4. Install local changes, refresh only authorized test connections/workers;
   test first-call registration and a claude39 round trip without dot prompts;
   full unit suite and independent review of failure/concurrency cases.

No new cloud service, guessed pane, global restart, or fabricated hook trust is
needed. Claude Channels remains an optional future transport because custom
channels currently need a separate development opt-in at process launch.

Sources checked: installed Codex 0.153.4 and Claude Code 2.1.259; OpenAI
`codex-rs/core/src/mcp_tool_call.rs` (rust-v0.153.4, threadId metadata),
https://code.claude.com/docs/en/channels-reference.

## Verification outcome

- New ephemeral Codex thread `01a07f88-16cf-73f1-861f-816e95a9727f` called
  native `mcpServer/tool/call(connection_status)` without registration. Result:
  `ready=true`, `identity_source=request_meta`, `auto_delivery=true`, `policy=idle`.
- Final new-thread native round trip used thread
  `01a07f9e-7d49-7042-89e7-6ff7b3c0f035`: question
  `1788847489194-7c5f6e` received Claude39 reply `1788847516892-afb3cc`
  (`FINAL-NATIVE-OK`) with the original question/conversation IDs preserved.
  The test deliberately disabled automatic model generation on that empty
  thread; actual automatic active/idle generation was verified separately in
  `reliable-conversations.md`. Both temporary addresses were removed and threads
  unsubscribed after testing.
- Claude39 replied `NO-DOT-OK` to `1788846217703-3acb27`. The live watchdog
  uses `message-envelope-v1`; no extra wake for that live exchange was recorded.
- Full unit suite: 140 passed (`tests`, excluding interactive `test_e2e.py`).
  This includes concurrent identity isolation, real-envelope wake, hook lease
  coordination, ambiguous delivery suppression and known-failure recovery.
