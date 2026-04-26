# Example — Bidirectional Chain

Claude is sketching an architecture and wants Codex to verify a low-level
detail. Codex in turn wants Claude to confirm the higher-level intent.

```
Claude:
  ask_codex("In our event-sourcing module, do we need fsync after every append?")

Codex (replies with answer + marker)
  Claude reads answer, decides to follow up:
  ask_codex("OK — and what about during compaction?")

Codex replies again. Claude is now satisfied and continues.
```

Both transcripts remain visible in their respective iTerm panes — no
hidden background magic, just files and `send_text` calls.

The AGENTS.md cap on chain depth is **3** to prevent runaway loops.
