"""Call one tool over a fresh MCP stdio connection after local code changes.

Usage: .venv/bin/python scripts/mcp_call.py TOOL '{"argument": "value"}'
This uses the MCP protocol, and never restarts another session's server.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def call(tool: str, arguments: dict) -> int:
    root = Path(__file__).resolve().parent.parent
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "teammate_mcp.cli", "serve"],
        cwd=str(root), env=dict(os.environ),
    )
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            result = await session.call_tool(tool, arguments)
            print(result.model_dump_json())
            failed = result.isError or any(
                getattr(item, "text", "").startswith(("ERROR:", "TIMEOUT:", "REFUSED:"))
                for item in result.content
            )
            if result.structuredContent and result.structuredContent.get("error"):
                failed = True
            for item in result.content:
                try:
                    parsed = json.loads(getattr(item, "text", ""))
                    if isinstance(parsed, dict) and parsed.get("error"):
                        failed = True
                except (ValueError, TypeError):
                    pass
            return 1 if failed else 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: mcp_call.py TOOL JSON_ARGUMENTS")
    raise SystemExit(asyncio.run(call(sys.argv[1], json.loads(sys.argv[2]))))
