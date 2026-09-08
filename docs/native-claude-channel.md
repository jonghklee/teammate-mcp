# Native Claude queue delivery

Requirement: a peer message enters Claude's execution queue while the human is
still editing a draft. The draft must not be cleared, copied back, submitted,
or otherwise used as the transport. An unsent draft has not entered the queue;
channel events can be processed before the human later submits it. Already
running turns are not interrupted; Claude controls native queue scheduling.

## Implemented

- Advertise the standard MCP experimental `claude/channel` capability.
- Send `notifications/claude/channel` over the MCP connection owned by the
  receiving Claude process. No keyboard, screen capture or AppleScript is used.
- Establish readiness with a nonce handshake that the receiving model must
  actually receive and acknowledge through `channel_ready` on that connection.
  Advertising a capability or successfully writing to stdio is not enough.
- Keep durable original IDs and route answers through `reply`. Mark transport
  writes as sent, not processed. Preserve uncertain dispatch rather than
  blindly resending. Do not recreate an inbox item after a fast receipt.
- Default pane keyboard delivery is disabled. The old implementation is only
  available under explicit `TEAMMATE_LEGACY_PANE_INPUT=1`; the channel launcher
  unsets that flag. Native channel mail is excluded from the old inbox hooks.

## Claude runtime prerequisite

Claude Code must enable this custom channel at process launch:

```sh
./bin/claude-channel
```

This invokes:

```sh
claude --dangerously-load-development-channels server:teammate
```

Claude presents its own local-development channel confirmation. This flag
does not bypass tool permissions or organization channel policy. Do not edit
Claude's approval records or simulate that confirmation. Existing sessions
started without the channel flag do not become opted in merely by editing
the server. Start a separate test session so the current draft stays intact.

Current Claude39 was observed running without a channel launch flag. Its
pending RECHECK message is preserved for native delivery, not re-injected into
its draft. No successful native-UI delivery has been claimed for that session.

## Live acceptance check (after channel opt-in)

1. Observe `connection_status` with channel state ready after the nonce event.
2. Type an unsent draft in the test Claude session and keep editing it.
3. Send a peer question with an independently chosen token.
4. Verify the channel event is processed and a correlated reply arrives while
   that draft remains intact. A screen/inbox read by the sender is not receipt.
5. Verify no extra Enter, dot, clear, paste or draft restoration occurred.

Source: https://code.claude.com/docs/en/channels-reference (capability,
notification format, queue behavior and runtime opt-in requirements).

## Verification recorded

150 unit/protocol tests passed, including raw MCP stdio initialization,
capability advertisement, nonce confirmation, notification delivery, original
job IDs, receipt processing, duplicate suppression and active-lease exclusion.
The raw-wire client is a test harness, not proof of a live Claude UI preserving
a draft. That acceptance check still requires a channel-enabled Claude session.
The installed Claude 2.1.259 reports authenticated first-party claude.ai access;
the existing Claude39 process was observed without the channel launch flag.
