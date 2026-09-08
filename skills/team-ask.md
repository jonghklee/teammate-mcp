---
name: team-ask
description: Use when the user asks to communicate with another named teammate, ask it a question, or verify a two-way conversation.
---

# Send a question and receive the answer

Use only the target and scope the user authorized. Ensure this session has a
real return address first: connection_status triggers automatic setup and reports
readiness. No separate registration command is normally needed.

Call MCP `ask(target=<label>, question=<text>)` and record its job ID.
`sent` or `queued` means transport acceptance, not that the other agent read,
answered, or completed the task. Never print that receipt as the answer.

Ask the receiver to use `reply(job_id=<question ID>, question=<answer>,
label=<its own label>)`. This preserves the conversation and question IDs,
routes to the original sender, and does not send a completed reply twice.
Read received messages through `inbox(label=<own label>)` or automatic thread
input. Match sender and in_reply_to; then acknowledge processing with
mark_processed. A receipt does not require another acknowledgement.

During a busy Codex turn, default idle delivery keeps new messages queued.
If this turn is waiting specifically for an answer, checking its own inbox is
appropriate. Prefer bounded waits and continue independent work. Use
mailbox_status to distinguish queued, waiting, delivered, retry, failed and
uncertain. retry_delivery can reset a known failed attempt; never blindly resend
uncertain messages. Claude receives through native channel events. Do not type, clear, restore or
submit its draft. If the channel is unavailable, report the setup requirement;
do not ask the user to finish typing as a transport workaround.

If the running MCP process has old code, invoke a fresh MCP client:

```sh
/Users/siheom-yong/programming/teammate-mcp/.venv/bin/python /Users/siheom-yong/programming/teammate-mcp/scripts/mcp_call.py TOOL JSON_ARGUMENTS
```

Use Python subprocess.run with an argument list and json.dumps for complex
text. Preserve the caller's actual identity; a registered pane may explicitly
set its own TEAMMATE_LABEL. Never forge the receiver's inbox or another
agent's reply. This client is an actual MCP protocol connection.

For bidirectional verification, have the other agent originate a question and
choose its own token. Receive it, answer it, and confirm its acknowledgement.
Reading the other pane's screen can diagnose failure, but does not prove a
direct message reached this session. Report manual polling and automatic
arrival separately. Keep the user's broader work active while handling peers.
