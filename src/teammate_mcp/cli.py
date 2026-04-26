"""Console-script entrypoint: `teammate-mcp [serve|register-pane|list|...]`."""

from __future__ import annotations

import asyncio
import json
import os
import sys

from . import __version__
from .queue import MessageQueue
from .server import main as serve_main


HELP = """\
teammate-mcp — inter-agent Q&A through iTerm panes

Usage:
  teammate-mcp                  start the MCP server (spawned by Claude/Codex)
  teammate-mcp serve            same as above, explicit
  teammate-mcp register-pane    register THIS shell's iTerm pane in the
                                registry under an auto-assigned label
                                (claude1, codex1, codex2, ...). Pure
                                program — no LLM round trip. Run this
                                BEFORE you launch claude/codex in the
                                same pane.
  teammate-mcp list             print every registered pane
  teammate-mcp unregister LBL   remove a label from the registry
  teammate-mcp status           print queue status as JSON
  teammate-mcp version          print version
  teammate-mcp help             this message

Environment variables:
  TEAMMATE_LABEL        explicit label override for register-pane
  TEAMMATE_QUEUE_MODE   ephemeral|audit  (default: ephemeral)
  TEAMMATE_CWD          override pane disambiguation cwd (default: $PWD)
  TEAMMATE_LOG_FILE     0|1  write JSONL log to ~/.teammate-mcp/logs/
  TEAMMATE_LOG_VERBOSE  0|1  echo log to stderr

Recommended workflow:
  alias tmclaude='teammate-mcp register-pane && claude'
  alias tmcodex='teammate-mcp register-pane && codex --yolo'
  # then in any pane: `tmclaude` or `tmcodex` — registered automatically
  # before the CLI starts; visible to other panes immediately.
"""


def _cmd_register_pane(argv: list[str]) -> int:
    """Register the calling shell's iTerm pane — no LLM in the loop."""
    explicit_label = ""
    if len(argv) > 0 and not argv[0].startswith("-"):
        explicit_label = argv[0]
    explicit_label = explicit_label or os.environ.get("TEAMMATE_LABEL", "").strip()

    tsid = os.environ.get("TERM_SESSION_ID", "")
    sid_tail = tsid.split(":", 1)[1] if ":" in tsid else tsid
    if not sid_tail:
        print("ERROR: no TERM_SESSION_ID — are you running inside iTerm?", file=sys.stderr)
        return 2

    async def _go():
        try:
            import iterm2
        except ImportError:
            print("ERROR: iterm2 python lib not installed", file=sys.stderr)
            return 2
        try:
            connection = await iterm2.Connection.async_create()
        except Exception as e:
            print(f"ERROR: cannot connect to iTerm Python API ({e}). "
                  f"Enable it in iTerm Settings → General → Magic.", file=sys.stderr)
            return 2

        from .iterm import list_sessions
        from .server import _next_auto_label
        from . import registry, spawn_track

        try:
            # iTerm's Python API can be slow to expose freshly-spawned
            # sessions. Retry the lookup up to 8 times with a 0.75s gap.
            me = None
            for attempt in range(8):
                refs = await list_sessions(connection)
                me = next(
                    (r for r in refs
                     if r.session_id.upper().endswith(sid_tail.upper())
                        or r.session_id.upper() == sid_tail.upper()),
                    None,
                )
                if me is not None:
                    break
                await asyncio.sleep(0.75)
            if me is None:
                print(f"ERROR: iTerm session {sid_tail} not found "
                      f"after retries", file=sys.stderr)
                return 2

            existing = next(
                (l for l, r in registry.all_labels().items()
                 if (r.get("session_id") or "").upper() == me.session_id.upper()),
                None,
            )
            label = explicit_label or existing or _next_auto_label(me.job, me.name or "")

            registry.register(
                label=label,
                session_id=me.session_id,
                pid=os.getpid(),
                job=me.job,
                cwd=me.cwd,
                extra={"session_name": me.name or None,
                       "auto_assigned": not explicit_label,
                       "via": "cli"},
            )
            spawn_track.record(me.session_id, spawned_by=f"cli register-pane (pid {os.getpid()})")

            # Set the iTerm tab title so the label is visible immediately.
            try:
                await me.session.async_send_text(f"\x1b]2;[{label}]\x07")
            except Exception:
                pass

            print(f"✓ registered as {label}  (session {me.session_id[:8]}…, "
                  f"job={me.job!r}, cwd={me.cwd})")
            return 0
        finally:
            try:
                connection.close()
            except Exception:
                pass

    return asyncio.run(_go())


def _cmd_list() -> int:
    from . import registry
    panes = registry.all_labels()
    if not panes:
        print("(no panes registered)")
        return 0
    print(f"{'LABEL':<14} {'JOB':<10} {'SESSION':<10} CWD")
    for label, rec in sorted(panes.items()):
        print(f"{label:<14} "
              f"{(rec.get('job') or '?'):<10} "
              f"{(rec.get('session_id') or '?')[:8]:<10} "
              f"{rec.get('cwd') or ''}")
    return 0


def _cmd_unregister(argv: list[str]) -> int:
    if not argv:
        print("usage: teammate-mcp unregister <label>", file=sys.stderr)
        return 2
    from . import registry
    registry.unregister(argv[0])
    print(f"✓ unregistered {argv[0]!r}")
    return 0


def _cmd_statusline() -> int:
    """Print this pane's teammate label for Claude Code's statusLine.

    Looks up the calling shell's TERM_SESSION_ID in the registry and
    emits a one-line summary. Claude Code reads stdin (a JSON blob with
    cwd/model/etc) on every turn and renders our stdout under the
    prompt. We ignore stdin and print whatever's most useful.
    """
    import json as _json
    # Drain stdin — Claude Code feeds us context; we don't need it here.
    try:
        if not sys.stdin.isatty():
            sys.stdin.read()
    except Exception:
        pass

    from . import registry
    tsid = os.environ.get("TERM_SESSION_ID", "")
    sid_tail = tsid.split(":", 1)[1] if ":" in tsid else tsid
    sid_up = sid_tail.upper() if sid_tail else ""

    label = None
    if sid_up:
        for lbl, rec in registry.all_labels().items():
            rec_sid = (rec.get("session_id") or "").upper()
            if rec_sid == sid_up or rec_sid.endswith(sid_up):
                label = lbl
                break

    # All currently registered labels (sorted) for context.
    others = sorted(registry.all_labels().keys())
    others_str = " ".join(others) if others else "(none)"

    if label:
        print(f"🟢 {label}  │ team: {others_str}")
    else:
        print(f"⚪ unregistered  │ team: {others_str}  │ run /team-register")
    return 0


def main():
    if len(sys.argv) <= 1 or sys.argv[1] == "serve":
        serve_main()
        return

    cmd = sys.argv[1]
    rest = sys.argv[2:]

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
    if cmd == "register-pane":
        sys.exit(_cmd_register_pane(rest))
    if cmd == "list":
        sys.exit(_cmd_list())
    if cmd == "unregister":
        sys.exit(_cmd_unregister(rest))
    if cmd == "statusline":
        sys.exit(_cmd_statusline())

    print(f"unknown subcommand: {cmd!r}", file=sys.stderr)
    print(HELP, file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
