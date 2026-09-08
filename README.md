# teammate-mcp

Local two-way conversations between Claude Code/iTerm sessions and Codex
threads. Messages have stable IDs, durable history and correlated replies.
A transport receipt is distinct from an answer.

## Setup

From this repository:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
claude mcp add --scope user teammate -- "$PWD/.venv/bin/teammate-mcp" serve
codex mcp add teammate -- "$PWD/.venv/bin/teammate-mcp" serve
```

For native Claude queue delivery, start Claude with `./bin/claude-channel` and
accept its local-development channel confirmation. This is required by Claude;
MCP registration alone does not enable channel input. Keep an existing draft
untouched by starting a separate channel session for the initial check.

Add the server to each client you use; do not add duplicate entries if it is
already configured. Existing processes need one teammate MCP reconnect to load
updated code. `bin/install-claude` additionally installs slash commands and
inbox hooks while preserving unrelated settings.

Verified environment: macOS, iTerm2, Claude Code 2.1.259, Codex 0.153.4.
iTerm delivery needs its automation/Python API access. Pane-free automatic
Codex delivery needs the local app-server control socket and a thread with
`canAcceptDirectInput=true`. There is no cross-machine relay in this package.

## Automatic registration

Normally no `/register` command is necessary:

- An iTerm session is identified at MCP startup and reuses its existing label.
- Current Codex attaches `_meta.threadId` to tool calls. The first call registers
  that exact thread and enables idle delivery. Identity is isolated per request,
  so a shared MCP process cannot mix concurrent callers through global env state.
- A standalone Codex CLI without a usable app-server transport keeps its verified
  physical pane address. No other pane is guessed from title or working directory.
- A reconnect keeps existing policy, including an explicit automatic-delivery disable.

A pane-free connection cannot be assigned before the client provides its identity;
its first tool invocation completes setup. Call `connection_status()` to see
address, transport, readiness, identity source and any setup error. Older clients
that provide neither pane identity nor thread metadata need explicit recovery
registration (`register_self` / `register_mailbox`).

## Conversation tools

| Tool | Purpose |
| --- | --- |
| `connection_status()` | Automatic setup and this caller's readiness |
| `list_panes()` | Live panes plus registered Codex mailbox addresses |
| `ask(target, question)` | Send a question to a registered label |
| `reply(job_id, question, label?)` | Answer the original sender with question/conversation IDs |
| `inbox(label?)` | Inspect this session's unprocessed messages |
| `mark_processed(job_id, target?, reply?)` | Record completion without another message |
| `mailbox_status(label)` | Inspect queued/waiting/delivered/retry/failed/uncertain state |
| `retry_delivery(job_id, label?)` | Retry a known failure for the receiving session |
| `configure_mailbox_delivery(label, policy="idle", enabled=true)` | Configure the owning Codex thread |
| `register_mailbox(label?, thread_id?)` | Explicit recovery/custom registration |
| `register_self(label?)` | Explicit registration for the calling environment |

Use explicit target labels. `sent`/`queued` is not a response. A reply includes
`in_reply_to` and `conversation_id`; an already-sent reply is not sent again.
If reply receipt writing fails, repeating the reply finishes the receipt without
repeating the send. Do not turn acknowledgements into an infinite ping-pong loop.

[Registration skill](skills/team-register.md) · [Conversation skill](skills/team-ask.md)

## Delivery and recovery

Codex: idle policy waits for the current turn to finish, then uses app-server
`turn/start`. Explicit immediate policy uses `turn/steer` for the matching active
turn. Messages remain durable until processed. A disconnected worker is restarted
on the next applicable tool call or send. Known pre-dispatch failures retry with
backoff; an ambiguous dispatch is never blindly resent.

Claude: MCP `notifications/claude/channel` events enter the native execution
queue independently of the draft editor. A nonce handshake must be received and
acknowledged by Claude before the channel is marked ready. No draft text is read,
cleared, restored or submitted. An unsent draft is not in the execution queue;
peer events can arrive before its later submission. Claude controls scheduling
of already-running turns and previously submitted messages.

Keyboard transport is disabled by default, including the watchdog. A channel
that is unavailable leaves mail queued with an explicit waiting state; it does
not fall back to typing into the editor. The old keyboard path remains only for
explicit compatibility testing under `TEAMMATE_LEGACY_PANE_INPUT=1`; the channel
launcher unsets it. See [native channel setup and acceptance](docs/native-claude-channel.md).

State lives under `~/.teammate-mcp/`: registry, per-address inbox/history/processed,
response records, delivery state and worker health. Inspect
`run/mailbox-worker.log` and `logs/watchdog.log` for diagnostic errors. A failed
or uncertain status is not silently called complete. `retry_delivery` refuses
accepted/ambiguous deliveries; inspect the recipient before any manual resend.

## Fresh MCP connection for old running clients

```sh
.venv/bin/python scripts/mcp_call.py connection_status '{}'
.venv/bin/python scripts/mcp_call.py ask '{"target":"claude39","question":"Hello"}'
```

This is an actual MCP `ClientSession.call_tool` connection, not direct file-based
message simulation. It preserves the caller's real environment. For complex
message text use a subprocess argument list and `json.dumps` rather than shell
string interpolation. A native current Codex tool call also supplies thread
metadata automatically; the helper is not required after reconnecting.

## Verification

```sh
.venv/bin/python -m pytest tests --ignore=tests/test_e2e.py -q
```

The separate iTerm sanity test needs an interactive desktop. Live two-way,
automatic active/idle delivery, word-chain and first-call registration evidence
is in [reliable conversations](docs/reliable-conversations.md) and
[automatic setup](docs/automatic-setup-plan.md).
