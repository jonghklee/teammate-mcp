# Example — Single Q&A

Open two iTerm panes in any window, run `claude` in one and `codex` in the
other (any order, any layout — `teammate-mcp` will find them by process
name). From inside Claude:

```
> mcp__teammate__ask_codex(question="What's a good Rust crate for PDF text extraction?")
```

Codex receives:

```
[teammate-mcp ASK 1714142400123-ab12cd from=claude]
What's a good Rust crate for PDF text extraction?

When you finish, output exactly this marker on its own line:
<<DONE_1714142400123-ab12cd>>
```

Codex composes its answer naturally and finishes with the marker line.
The MCP server captures everything between the question and the marker
and hands it back to Claude as the tool result.
