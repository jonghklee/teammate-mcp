"""Authoritative per-request identity, isolated across concurrent MCP calls."""
from contextlib import contextmanager
from contextvars import ContextVar
import os
import re

_thread = ContextVar("teammate_request_thread", default=None)


def request_thread_id():
    return _thread.get()


def current_thread_id():
    return _thread.get() or os.environ.get("CODEX_THREAD_ID", "").strip()


@contextmanager
def request_identity(meta):
    if hasattr(meta, "model_dump"):
        meta = meta.model_dump(exclude_none=True)
    thread_id = (meta or {}).get("threadId")
    if thread_id is not None and (
        not isinstance(thread_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,100}", thread_id)
    ):
        raise ValueError("invalid authoritative MCP threadId")
    token = _thread.set(thread_id)
    try:
        yield
    finally:
        _thread.reset(token)
