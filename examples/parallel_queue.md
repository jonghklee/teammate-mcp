# Example — Concurrent Asks Are Queued

If Claude fires three `ask_codex` calls back to back, they are written
into `pending/` with millisecond-precision IDs and consumed in FIFO
order. Each call blocks its caller until that specific marker appears,
so the *answer ordering* matches the *enqueue ordering* even if you
launched them from parallel tool blocks.

The atomic `pending/ → inflight/` rename guarantees that exactly one
worker handles each message; if you ever extend this to multiple Codex
panes (e.g. `codex-1`, `codex-2`), the same lock protocol applies.

In ephemeral mode the queue lives under `/tmp/teammate-mcp-XXXX/` and
disappears when the MCP server exits. Switch to `audit` mode by setting
`TEAMMATE_QUEUE_MODE=audit` to keep `.queue/` under your project root.
