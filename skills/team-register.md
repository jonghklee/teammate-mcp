---
name: team-register
description: Use when the user asks to register, identify or label the current teammate session, including a pane-free Codex thread.
---

# Automatic session setup

Call MCP `connection_status` first. Current Codex sends its actual threadId in
request metadata: the server automatically registers and enables idle delivery
on the first tool call. iTerm sessions are identified at server startup. Report
the returned address, transport, ready flag and any setup_error. Reconnection
reuses the address and preserves an explicitly disabled delivery setting.

Do not require the user to run register-pane on every session. If a user chooses
a custom label, use register_self for a pane or register_mailbox for a thread.
Do not assign another pane just because its title or cwd is similar.

For old running MCP processes, reconnect the teammate server once. Until then a
fresh actual MCP stdio client can call the updated tools:

```sh
/Users/siheom-yong/programming/teammate-mcp/.venv/bin/python /Users/siheom-yong/programming/teammate-mcp/scripts/mcp_call.py connection_status '{}'
```

If identity is unavailable, inspect TERM_SESSION_ID, CODEX_THREAD_ID and actual
process ancestry. The explicit register-pane CLI or register_mailbox tool is
for recovery/older clients, not the normal workflow. Never invent a thread ID.

MCP connection alone cannot reveal a pane-free thread before the client sends
its identity. Automatic setup happens on its first tool call. A missing local
app-server transport is reported as manual/unavailable, not automatic readiness.
If the request also asks for a conversation, continue through actual reception.

## Claude native queue requirement

Claude must be launched with the teammate channel enabled (bin/claude-channel).
Its own local-development approval is required. The nonce channel handshake
proves notifications actually reach this session; do not mark ready just because
MCP tools are connected. A normal old session needs a channel-enabled launch,
not merely an MCP reconnect. Never clear or submit an existing draft to enable it.
