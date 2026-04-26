"""Console-script entrypoint: `teammate-mcp [serve|status|version]`."""

from __future__ import annotations

import json
import sys

from . import __version__
from .queue import MessageQueue
from .server import main as serve_main


HELP = """\
teammate-mcp — inter-agent Q&A through iTerm panes

Usage:
  teammate-mcp                  start the MCP server (default; spawned by Claude/Codex)
  teammate-mcp serve            same as above, explicit
  teammate-mcp status           print queue status as JSON and exit
  teammate-mcp version          print version
  teammate-mcp help             this message

Environment variables:
  TEAMMATE_QUEUE_MODE   ephemeral|audit  (default: ephemeral)
  TEAMMATE_CWD          override pane disambiguation cwd (default: $PWD)
  TEAMMATE_LOG_FILE     0|1  write JSONL log to ~/.teammate-mcp/logs/ (default: 1)
  TEAMMATE_LOG_VERBOSE  0|1  echo log to stderr (default: 1)
"""


def main():
    if len(sys.argv) <= 1 or sys.argv[1] == "serve":
        serve_main()
        return

    cmd = sys.argv[1]
    if cmd == "version":
        print(__version__)
        return
    if cmd in ("help", "-h", "--help"):
        print(HELP)
        return
    if cmd == "status":
        q = MessageQueue(mode="audit")
        print(json.dumps(q.status(), indent=2))
        return

    print(f"unknown subcommand: {cmd!r}", file=sys.stderr)
    print(HELP, file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
