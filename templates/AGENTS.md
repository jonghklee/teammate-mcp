# Teammate operating rules

<!-- TEAMMATE_MCP_START -->
## Teammate conversations

Use teammate MCP connection_status to identify this session. Current Codex
requests register their thread automatically; iTerm sessions register at MCP
startup. A missing identity is an error, not permission to pick another pane.

**Preferred path — MCP tool:**

Use ask(target=<registered label>, question=<message>) to ask a peer. The
returned job ID means accepted/queued, not answered. Use reply(job_id,
question=<answer>) for a received question so routing and correlation survive.
Process received answers with mark_processed. Never automatically acknowledge
an acknowledgement. Match sender and in_reply_to when awaiting a result.

Idle Codex delivery waits for the current turn to finish, then starts a turn.
If actively waiting for a peer answer, a bounded own-inbox check is appropriate.
Use mailbox_status and retry_delivery for known failures. Uncertain input is
not automatically resent. The pane watchdog sends actual messages, never dots.

An old MCP process needs one reconnect to load updated tools. The repository's
scripts/mcp_call.py opens a fresh actual MCP stdio connection when needed.
Do not fabricate inbox files or treat reading a peer screen as direct reception.
<!-- TEAMMATE_MCP_END -->
