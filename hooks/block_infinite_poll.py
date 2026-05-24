#!/usr/bin/env python3
"""Compatibility wrapper for the packaged PreToolUse hook."""

from teammate_mcp.hooks.block_infinite_poll import evaluate_command, main


if __name__ == "__main__":
    raise SystemExit(main())
