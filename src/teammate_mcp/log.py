"""Structured JSON-line logger with monotonic elapsed timing.

Output is written to stderr (so it doesn't pollute MCP stdout) and,
optionally, to a daily file under ``~/.teammate-mcp/logs/``.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, TextIO


class Logger:
    def __init__(self, also_file: bool = True, verbose: bool = True):
        self.also_file = also_file
        self.verbose = verbose
        self._file: Optional[TextIO] = None
        self._t0 = time.monotonic()
        if also_file:
            log_dir = Path.home() / ".teammate-mcp" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            self._file = open(log_dir / f"{today}.jsonl", "a", encoding="utf-8")

    def event(self, name: str, **fields) -> None:
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "elapsed_ms": int((time.monotonic() - self._t0) * 1000),
            "event": name,
        }
        rec.update(fields)
        line = json.dumps(rec, ensure_ascii=False)
        if self.verbose:
            print(line, file=sys.stderr, flush=True)
        if self._file is not None:
            self._file.write(line + "\n")
            self._file.flush()

    def close(self) -> None:
        if self._file is not None:
            self._file.close()


_global_logger: Optional[Logger] = None


def get_logger() -> Logger:
    global _global_logger
    if _global_logger is None:
        _global_logger = Logger(
            also_file=os.environ.get("TEAMMATE_LOG_FILE", "1") != "0",
            verbose=os.environ.get("TEAMMATE_LOG_VERBOSE", "1") != "0",
        )
    return _global_logger
