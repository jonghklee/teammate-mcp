"""Smoke test — dependencies are importable, package metadata is intact."""

from __future__ import annotations


def test_package_imports():
    import teammate_mcp
    assert teammate_mcp.__version__


def test_iterm_module_imports():
    # iterm2 itself should be present even if API not enabled.
    import iterm2  # noqa: F401
    from teammate_mcp import iterm
    assert hasattr(iterm, "find_session_by_job")
    assert hasattr(iterm, "send_text")
    assert hasattr(iterm, "wait_for_marker")


def test_queue_module_imports():
    from teammate_mcp.queue import MessageQueue
    q = MessageQueue(mode="ephemeral")
    assert q.mode == "ephemeral"
    assert q.base.exists()


def test_log_module_imports():
    from teammate_mcp.log import Logger
    log = Logger(also_file=False, verbose=False)
    log.event("smoke", ok=True)
    log.close()


def test_server_module_imports():
    # Importing the FastMCP module should not require an iTerm
    # connection — only tool invocations do.
    from teammate_mcp import server
    assert server.mcp is not None
