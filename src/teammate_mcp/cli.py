"""Console-script entrypoint: `teammate-mcp [serve|register-pane|list|...]`."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Optional

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
                                Aliases: `register`, `reg`
  teammate-mcp list             print every registered pane
  teammate-mcp whoami           print THIS pane's label (or "(unregistered)")
  teammate-mcp exists LBL       exit 0 if LBL is registered, 1 if not
  teammate-mcp ask LBL Q...     ask LBL the question Q.
                                Default ASYNC / mailbox; --wait for sync.
                                For bodies > ~500KB use --stdin or --body-file
                                to avoid OS argv limit (ARG_MAX).
                                  echo "<huge>" | teammate-mcp ask --stdin LBL
                                  teammate-mcp ask --body-file <path> LBL
  teammate-mcp inbox [LBL]      list pending mailbox entries for LBL
                                (defaults to caller's own pane label)
  teammate-mcp mark-processed   close a sync ask: write processed/<id>.json
        <id> [--reply "..."]    with a reply field. (Aliases: ack, mark)
        [--target LBL]
  teammate-mcp drain [LBL]      run the inbox drain logic now and print
                                pending mail (useful when MCP is dead
                                or you don't want to type a prompt)
  teammate-mcp prune            remove every registry entry whose iTerm
                                session is no longer open (also auto-runs
                                inside `list` and `register-pane`)
  teammate-mcp watch [--once]   watchdog: poll mailboxes, wake idle
        [--interval N]          Claude Code panes by injecting /drain
                                (only when compose box looks empty).
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
  TEAMMATE_INJECT       0|1  also inject the body via keystrokes (best-effort
                              wake for receivers without the v0.7+ hook).
                              WARNING: re-introduces compose-merge risk —
                              only set this when you know the receiver
                              cannot drain via hook (codex panes, legacy
                              sessions). Default: off.

Recommended workflow:
  alias tmclaude='teammate-mcp register-pane && claude'
  alias tmcodex='teammate-mcp register-pane && codex --yolo'
  # then in any pane: `tmclaude` or `tmcodex` — registered automatically
  # before the CLI starts; visible to other panes immediately.
"""


def _osa_session_info(sid_tail: str) -> Optional[dict]:
    """Query iTerm via osascript to fetch the calling pane's info.

    Used as a fallback when the iterm2 Python lib cannot connect — most
    notably when the caller is running inside a macOS App Sandbox (codex
    CLI's bash tool) which blocks Unix-socket connect() with EPERM.
    AppleScript is delivered through a separate osascript process whose
    Apple Event channel is not subject to the caller's sandbox.
    """
    import subprocess
    # iTerm's `tty` is the most reliable bridge: TERM_SESSION_ID's tail
    # equals the session's `unique id`. `cwd of session` only exists on
    # newer iTerm; fall back to ~ if missing.
    script = f'''
tell application "iTerm"
    repeat with w in windows
        repeat with t in tabs of w
            repeat with s in sessions of t
                if (unique id of s) is "{sid_tail.upper()}" then
                    set sName to name of s
                    set sTTY to tty of s
                    set sId  to unique id of s
                    return sId & "|" & sName & "|" & sTTY
                end if
            end repeat
        end repeat
    end repeat
    return ""
end tell
'''
    try:
        out = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=8,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    line = (out.stdout or "").strip()
    if not line or "|" not in line:
        return None
    parts = line.split("|")
    if len(parts) < 3:
        return None
    sid, name, tty = parts[0], parts[1], parts[2]
    # Resolve job + cwd from the tty's foreground process.
    job, cwd = _proc_info_for_tty(tty)
    return {"session_id": sid, "name": name, "tty": tty,
            "job": job, "cwd": cwd}


def _proc_info_for_tty(tty: str) -> tuple[str, str]:
    """Return (job, cwd) of the foreground process on a tty.

    Pure shell — no iterm2 lib. Walks ``ps`` output for processes
    attached to the tty, picks the most recently started one (typically
    the running CLI), then asks ``lsof`` for its cwd.
    """
    import subprocess
    tty_short = tty.replace("/dev/", "")
    try:
        ps = subprocess.run(
            ["ps", "-t", tty_short, "-o", "pid=,stat=,command="],
            capture_output=True, text=True, timeout=3,
        )
    except Exception:
        return ("?", os.path.expanduser("~"))
    pids = []
    for ln in ps.stdout.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        # Foreground processes have '+' in stat
        parts = ln.split(None, 2)
        if len(parts) < 3:
            continue
        pid_s, stat, cmd = parts
        if "+" in stat:
            pids.append((int(pid_s), cmd))
    # Prefer the LAST foreground process (deepest child)
    if not pids:
        return ("?", os.path.expanduser("~"))
    pid, cmd = pids[-1]
    # Job name = first whitespace-separated token of cmd, basename only
    first = cmd.split()[0] if cmd else "?"
    job = os.path.basename(first.split("/")[-1])
    # cwd via lsof
    try:
        lsof = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True, text=True, timeout=3,
        )
        cwd = os.path.expanduser("~")
        for ln in lsof.stdout.splitlines():
            if ln.startswith("n"):
                cwd = ln[1:]
                break
    except Exception:
        cwd = os.path.expanduser("~")
    return (job, cwd)


def _cmd_register_pane(argv: list[str]) -> int:
    """Register the calling shell's iTerm pane — no LLM in the loop.

    Two-layer connection strategy:
      (A) iterm2 Python lib via Unix socket — fast, full-featured
      (B) osascript fallback — works when (A) is blocked by sandbox
          (codex CLI's bash tool gets EPERM on the socket connect)
    """
    explicit_label = ""
    if len(argv) > 0 and not argv[0].startswith("-"):
        explicit_label = argv[0]
    explicit_label = explicit_label or os.environ.get("TEAMMATE_LABEL", "").strip()

    tsid = os.environ.get("TERM_SESSION_ID", "")
    sid_tail = tsid.split(":", 1)[1] if ":" in tsid else tsid
    if not sid_tail:
        print("ERROR: no TERM_SESSION_ID — are you running inside iTerm?", file=sys.stderr)
        return 2

    # Auto-prune stale entries before this register so dead claudeN/codexN
    # numbers are recycled instead of monotonically growing. The prune
    # also archives the dead labels' mailboxes so the new register
    # never inherits a stale inbox.
    try:
        from . import registry as _reg
        _reg.prune_dead(force_refresh=True)
    except Exception:
        pass
    # Belt-and-suspenders: also archive any mailbox that already exists
    # for the label we're about to use — covers the race where prune
    # didn't run (iTerm fully restarted, alive set was empty, prune
    # was a no-op) but the user still wants a fresh inbox under the
    # same label.
    if explicit_label:
        try:
            from .server import archive_label_mailbox
            archive_label_mailbox(explicit_label)
        except Exception:
            pass

    # ---- Path B: osascript fallback (handles sandboxed callers) -----
    def _register_via_osascript() -> int:
        info = _osa_session_info(sid_tail)
        if info is None:
            print("ERROR: osascript fallback could not locate iTerm session "
                  f"{sid_tail}. Ensure iTerm is running and the pane is open.",
                  file=sys.stderr)
            return 2
        from .server import _next_auto_label
        from . import registry, spawn_track
        existing = next(
            (l for l, r in registry.all_labels().items()
             if (r.get("session_id") or "").upper() == info["session_id"].upper()),
            None,
        )
        label = explicit_label or existing or _next_auto_label(info["job"], info["name"] or "")
        registry.register(
            label=label,
            session_id=info["session_id"],
            pid=os.getpid(),
            job=info["job"],
            cwd=info["cwd"],
            extra={"session_name": info["name"] or None,
                   "auto_assigned": not explicit_label,
                   "via": "cli-osa-fallback"},
        )
        spawn_track.record(info["session_id"],
                           spawned_by=f"cli register-pane fallback (pid {os.getpid()})")
        # Only emit visibility escape sequences when stdout is a real TTY
        # (i.e. running pre-CLI from a plain shell). When stdout is a pipe
        # — the case when codex/claude's bash tool captures our output —
        # the ANSI banner becomes garbage in the agent's UI block and the
        # OSC sequences are stuck in the pipe (no live rendering anyway).
        if sys.stdout.isatty():
            import base64 as _b64
            badge_b64 = _b64.b64encode(label.encode()).decode()
            esc = (
                f"\x1b]0;[{label}]\x07"
                f"\x1b]1;[{label}]\x07"
                f"\x1b]2;[{label}]\x07"
                f"\x1b]1337;SetBadgeFormat={badge_b64}\x07"
                f"\x1b]1337;SetUserVar=teammate_label={badge_b64}\x07"
            )
            sys.stdout.write(esc)
            sys.stdout.write(
                f"\x1b[1;7;36m  ▌ teammate-mcp ▌  this pane = {label}  \x1b[0m\n"
            )
            sys.stdout.flush()
        print(f"✓ registered as {label}  (session {info['session_id'][:8]}…, "
              f"job={info['job']!r}, cwd={info['cwd']})  [osa fallback]")
        return 0

    async def _go():
        try:
            import iterm2
        except ImportError:
            print("ERROR: iterm2 python lib not installed", file=sys.stderr)
            return 2
        try:
            connection = await iterm2.Connection.async_create()
        except Exception as e:
            # Sandbox / EPERM / iTerm not running — try osascript path.
            err = str(e)
            if "Operation not permitted" in err or "Errno 1" in err:
                return _register_via_osascript()
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

            # Visibility — only fire ANSI/OSC sequences when our stdout
            # is a real terminal (running pre-CLI from a plain shell).
            # When stdout is a pipe (codex/claude bash tool capturing
            # the output), the escape bytes either get rendered as
            # garbage in the agent UI or get stuck in the pipe — they
            # never reach iTerm's live parser anyway. Skipping them
            # avoids the "input prompt looks broken after register" bug.
            if sys.stdout.isatty():
                import base64 as _b64
                badge_b64 = _b64.b64encode(label.encode()).decode()
                esc = "".join([
                    f"\x1b]0;[{label}]\x07",
                    f"\x1b]1;[{label}]\x07",
                    f"\x1b]2;[{label}]\x07",
                    f"\x1b]1337;SetBadgeFormat={badge_b64}\x07",
                    f"\x1b]1337;SetUserVar=teammate_label={badge_b64}\x07",
                ])
                # async_send_text wraps in bracket-paste — for a TUI like
                # codex/claude this leaves literal escape bytes inside the
                # input box. Only safe to inject when no TUI is running yet.
                try:
                    await me.session.async_send_text(esc)
                except Exception:
                    pass
                sys.stdout.write(esc)
                sys.stdout.write(
                    f"\x1b[1;7;36m  ▌ teammate-mcp ▌  this pane = {label}  \x1b[0m\n"
                )
                sys.stdout.flush()

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
    # Auto-prune: drop entries whose iTerm session is gone. Cheap
    # (one AppleScript call, cached for 5 s) so safe to call here.
    pruned = registry.prune_dead(force_refresh=True)
    panes = registry.all_labels()
    if not panes:
        print("(no panes registered)")
        if pruned:
            print(f"  (auto-pruned {len(pruned)} stale entries: {', '.join(sorted(pruned))})")
        return 0
    print(f"{'LABEL':<14} {'JOB':<10} {'SESSION':<10} CWD")
    for label, rec in sorted(panes.items()):
        print(f"{label:<14} "
              f"{(rec.get('job') or '?'):<10} "
              f"{(rec.get('session_id') or '?')[:8]:<10} "
              f"{rec.get('cwd') or ''}")
    if pruned:
        print(f"\n(auto-pruned {len(pruned)} stale entries: {', '.join(sorted(pruned))})")
    return 0


def _cmd_prune() -> int:
    """Explicit prune — remove every registry entry whose iTerm session
    is no longer open in iTerm. Same logic as the auto-prune that runs
    on `list` and `register-pane`.
    """
    from . import registry
    removed = registry.prune_dead(force_refresh=True)
    if not removed:
        print("(nothing to prune — all registered panes still live)")
        return 0
    print(f"✓ pruned {len(removed)} stale entries:")
    for label in sorted(removed):
        print(f"  - {label}")
    return 0


def _cmd_unregister(argv: list[str]) -> int:
    if not argv:
        print("usage: teammate-mcp unregister <label>", file=sys.stderr)
        return 2
    from . import registry
    registry.unregister(argv[0])
    print(f"✓ unregistered {argv[0]!r}")
    return 0


def _cmd_whoami() -> int:
    """Print the label of the calling pane (or "(unregistered)").

    Resolves the calling shell's TERM_SESSION_ID against the registry.
    Useful inside an agent: "내가 누구야?" → bash → teammate-mcp whoami.
    """
    from . import registry
    tsid = os.environ.get("TERM_SESSION_ID", "")
    sid_tail = (tsid.split(":", 1)[1] if ":" in tsid else tsid).upper()
    if not sid_tail:
        print("(no TERM_SESSION_ID — not running inside iTerm)")
        return 2
    for label, rec in registry.all_labels().items():
        rec_sid = (rec.get("session_id") or "").upper()
        if rec_sid == sid_tail or rec_sid.endswith(sid_tail) or sid_tail.endswith(rec_sid):
            print(label)
            return 0
    print("(unregistered)")
    return 1


def _cmd_ask(argv: list[str]) -> int:
    """One-shot ask: send a question to a registered pane.

    Usage (default is ASYNC / mailbox mode as of v0.8.0):
        teammate-mcp ask <label> <question...>             # async, file-only
        teammate-mcp ask --wait <label> <question...>      # legacy sync
        teammate-mcp ask --timeout 60 --wait <label> <question...>

    Flags:
        --wait         legacy sync mode (keystroke injection + marker poll).
                       Use only when the caller cannot proceed without the
                       inline reply. Will MERGE with text the user is
                       mid-typing in the target's compose box.
        --no-wait      explicit async (the default; included for clarity).
        --async        alias for --no-wait.
        --timeout N    sync timeout in seconds (default 300; ignored when async).
        -t N           alias for --timeout.

    Async mode persists the message to
    ``~/.teammate-mcp/mailbox/<label>/inbox/`` and returns immediately.
    The target's UserPromptSubmit hook drains the inbox on its next
    user prompt; the target then replies via reverse async ask.
    """
    timeout = 300
    wait = False  # v0.8.0: default async (mailbox/file-only delivery)
    body_from_stdin = False
    body_from_file = None
    # Two-pass: extract flags from anywhere in argv, leaving positional
    # args (label + question words) intact. This is forgiving of LLM
    # output that puts --async/--timeout after the label.
    args = []
    src = list(argv)
    while src:
        a = src.pop(0)
        if a in ("--timeout", "-t"):
            if not src:
                print("usage: teammate-mcp ask --timeout N <label> <question...>",
                      file=sys.stderr)
                return 2
            try:
                timeout = int(src.pop(0))
            except ValueError:
                print(f"ERROR: --timeout must be an integer", file=sys.stderr)
                return 2
        elif a in ("--no-wait", "--async"):
            wait = False
        elif a == "--wait":
            wait = True
        elif a.startswith("--timeout="):
            try:
                timeout = int(a.split("=", 1)[1])
            except ValueError:
                print(f"ERROR: --timeout must be an integer", file=sys.stderr)
                return 2
        elif a in ("--stdin", "--body-from-stdin"):
            # Read body from stdin — bypasses ARG_MAX (the OS limit
            # on argv length, which on macOS hits ~1MB and breaks
            # CLI invocations with large bodies). Receiver still
            # gets the full text.
            body_from_stdin = True
        elif a in ("--body-file",) and src:
            body_from_file = src.pop(0)
        elif a.startswith("--body-file="):
            body_from_file = a.split("=", 1)[1]
        elif a == "--":
            args.extend(src)
            break
        else:
            args.append(a)
    if (body_from_stdin or body_from_file):
        if not args:
            print("usage: teammate-mcp ask --stdin <label>  (then pipe body)",
                  file=sys.stderr)
            return 2
        target = args[0]
        if body_from_stdin:
            question = sys.stdin.read().strip()
        else:
            try:
                with open(body_from_file, encoding="utf-8") as f:
                    question = f.read().strip()
            except Exception as e:
                print(f"ERROR: cannot read --body-file: {e}", file=sys.stderr)
                return 2
        if not question:
            print("ERROR: empty body from stdin/file", file=sys.stderr)
            return 2
    else:
        if len(args) < 2:
            print("usage: teammate-mcp ask [--timeout N] [--no-wait] [--stdin] [--body-file PATH] <label> <question...>",
                  file=sys.stderr)
            return 2
        target = args[0]
        question = " ".join(args[1:]).strip()
        if not question:
            print("ERROR: empty question", file=sys.stderr)
            return 2

    from .server import _ask_async
    answer = asyncio.run(_ask_async(question=question, timeout=timeout,
                                     target=target, wait=wait))
    print(answer)
    if answer.startswith("ERROR:") or answer.startswith("TIMEOUT:") or answer.startswith("REFUSED:"):
        return 1
    return 0


def _cmd_mark_processed(argv: list[str]) -> int:
    """Mark an inbox message as processed (closes a sync caller's poll).

    Usage:
        teammate-mcp mark-processed <job_id> [--reply "..."] [--target LBL]

    Receivers whose MCP server has been killed (or who never had MCP)
    can still close a sync ask loop via this CLI: write
    ``processed/<job_id>.json`` with a ``terminal.reply`` field that
    the original sender's poll picks up.
    """
    if not argv:
        print("usage: teammate-mcp mark-processed <job_id> [--reply '…'] [--target LBL]",
              file=sys.stderr)
        return 2
    job_id = argv[0]
    reply = ""
    target = ""
    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--reply" and i + 1 < len(argv):
            reply = argv[i + 1]
            i += 2
        elif a.startswith("--reply="):
            reply = a.split("=", 1)[1]
            i += 1
        elif a == "--target" and i + 1 < len(argv):
            target = argv[i + 1]
            i += 2
        elif a.startswith("--target="):
            target = a.split("=", 1)[1]
            i += 1
        else:
            print(f"ERROR: unknown arg {a!r}", file=sys.stderr)
            return 2

    if not target:
        # Resolve caller's own label from TERM_SESSION_ID (sync mode
        # receivers default to their own mailbox).
        target = os.environ.get("TEAMMATE_LABEL", "").strip()
        if not target:
            from . import registry
            tsid = os.environ.get("TERM_SESSION_ID", "")
            sid_tail = (tsid.split(":", 1)[1] if ":" in tsid else tsid).upper()
            for lbl, rec in registry.all_labels().items():
                rec_sid = (rec.get("session_id") or "").upper()
                if sid_tail and (rec_sid == sid_tail or rec_sid.endswith(sid_tail)):
                    target = lbl
                    break
        if not target:
            print("ERROR: no --target given and could not resolve caller label",
                  file=sys.stderr)
            return 2

    from .server import _move_to_processed, _now_iso
    try:
        _move_to_processed(target, job_id,
                           {"status": "completed", "reply": reply,
                            "finished_at": _now_iso()})
        print(f"✓ {job_id} marked processed for {target}")
        if reply:
            print(f"  reply: {reply[:80]!r}")
        return 0
    except Exception as e:
        print(f"ERROR: {e!r}", file=sys.stderr)
        return 1


def _cmd_drain(argv: list[str]) -> int:
    """Run the inbox-drain logic on this pane and print the messages.

    Useful when:
      - the receiver's MCP is dead and the user wants to inspect mail
      - testing the hook output without submitting a real prompt
    """
    from . import registry
    label = (argv[0] if argv else "").strip()
    if not label:
        label = os.environ.get("TEAMMATE_LABEL", "").strip()
        if not label:
            tsid = os.environ.get("TERM_SESSION_ID", "")
            sid_tail = (tsid.split(":", 1)[1] if ":" in tsid else tsid).upper()
            for lbl, rec in registry.all_labels().items():
                rec_sid = (rec.get("session_id") or "").upper()
                if sid_tail and (rec_sid == sid_tail or rec_sid.endswith(sid_tail)):
                    label = lbl
                    break
    if not label:
        print("ERROR: no label given and could not resolve caller", file=sys.stderr)
        return 2

    # Re-use the same code path as the hook by spawning it.
    from pathlib import Path
    hook = (Path(__file__).resolve().parent.parent.parent
            / "hooks" / "user_prompt_submit_inbox_drain.py")
    if not hook.exists():
        # Fallback: inline using server helpers
        from .server import _list_inbox, _move_to_processed, _now_iso
        items = _list_inbox(label)
        if not items:
            print(f"(empty inbox for {label})")
            return 0
        for d in items:
            print(f"[{d.get('job_id','')[:18]}] from={d.get('from_')}: {d.get('body','')[:120]}")
            _move_to_processed(label, d["job_id"],
                               {"status": "drained_via_cli", "finished_at": _now_iso()})
        return 0

    import subprocess
    r = subprocess.run([str(hook)], input="{}", capture_output=True, text=True,
                       env={**os.environ, "TEAMMATE_LABEL": label}, timeout=5)
    print(r.stdout)
    return 0


def _cmd_inbox(argv: list[str]) -> int:
    """List pending mailbox entries for a label.

    Usage: teammate-mcp inbox [<label>]

    With no label, uses the caller's own pane label resolved via
    TERM_SESSION_ID against the registry.
    """
    label = argv[0] if argv else ""
    if not label:
        from .server import _osa_session_info as _osi  # noqa: F401  (may not exist)
        from . import registry
        tsid = os.environ.get("TERM_SESSION_ID", "")
        sid_tail = (tsid.split(":", 1)[1] if ":" in tsid else tsid).upper()
        for lbl, rec in registry.all_labels().items():
            rec_sid = (rec.get("session_id") or "").upper()
            if sid_tail and (rec_sid == sid_tail or rec_sid.endswith(sid_tail)):
                label = lbl
                break
    if not label:
        print("ERROR: no label given and could not resolve caller", file=sys.stderr)
        return 2
    from .server import _list_inbox
    items = _list_inbox(label)
    if not items:
        print(f"(empty inbox for {label})")
        return 0
    for entry in items:
        ts = entry.get("created_at", "?")
        frm = entry.get("from_", "?")
        jid = entry.get("job_id", "?")
        body = (entry.get("body") or "").replace("\n", " ")
        if len(body) > 80:
            body = body[:77] + "…"
        print(f"{ts}  [{jid[:18]}…]  {frm:>10} → {label:<10}  {body}")
    return 0


def _cmd_exists(argv: list[str]) -> int:
    """Check whether a teammate label exists. Exit 0 if yes, 1 if no.

    Usage: teammate-mcp exists <label>
    Prints either "yes <label> (session id, job, cwd)" or "no".
    """
    if not argv:
        print("usage: teammate-mcp exists <label>", file=sys.stderr)
        return 2
    target = argv[0]
    from . import registry
    rec = registry.all_labels().get(target)
    if rec is None:
        # Try case-insensitive fallback for ergonomics.
        for label, r in registry.all_labels().items():
            if label.lower() == target.lower():
                rec = r
                target = label
                break
    if rec is None:
        print(f"no  ({target!r} not in registry)")
        return 1
    sid = (rec.get("session_id") or "")[:8]
    print(f"yes  {target}  (session {sid}…, job={rec.get('job','?')!r}, "
          f"cwd={rec.get('cwd','?')})")
    return 0


def _cmd_install_iterm() -> int:
    """Set up everything iTerm needs in one shot.

    1. Drop the StatusBarComponent script into iTerm's AutoLaunch
       directory so it loads on every iTerm start.
    2. Drop a Dynamic Profile JSON ("Teammate") into iTerm's
       DynamicProfiles directory. The profile inherits the Default
       profile's look but enables the status bar so the user only has
       to drag our component into the layout once.
    3. Print a 3-line GUI checklist for the only manual step that the
       iTerm Python API can't automate (RPC component selection).
    """
    import shutil
    import json as _json
    import uuid as _uuid
    from pathlib import Path

    autolaunch_dir = Path.home() / "Library" / "Application Support" / "iTerm2" / "Scripts" / "AutoLaunch"
    dyn_dir = Path.home() / "Library" / "Application Support" / "iTerm2" / "DynamicProfiles"
    autolaunch_dir.mkdir(parents=True, exist_ok=True)
    dyn_dir.mkdir(parents=True, exist_ok=True)

    # 1. AutoLaunch script (overwrite — we ship the canonical version)
    here = Path(__file__).resolve().parent.parent.parent
    src = here / "iterm_autolaunch" / "teammate_label.py"
    if not src.exists():
        # Fall back: write inline copy from the package
        src = autolaunch_dir / "teammate_label.py"
        if not src.exists():
            print(f"WARN: AutoLaunch script template not found at {src}.")
    target_script = autolaunch_dir / "teammate_label.py"
    if src != target_script and src.exists():
        shutil.copy2(src, target_script)
    print(f"✓ AutoLaunch script: {target_script}")

    # 2. Dynamic Profile JSON — including the full Status Bar Layout
    # with our RPC component pre-installed. Built by encoding a
    # protobuf RPCRegistrationRequest, exactly as iTerm2 does
    # internally (see sources/StatusBar/Components/iTermStatusBar
    # RPCProvidedTextComponent.m, key "registration request v2").
    import base64 as _b64
    try:
        from iterm2 import api_pb2
    except ImportError:
        print("WARN: iterm2 protobuf not available — skipping Status Bar")
        print("Layout pre-install. Status bar will be enabled but the user")
        print("will still need to drag the component manually.")
        layout_components = []
    else:
        req = api_pb2.RPCRegistrationRequest()
        req.name = "teammate_label_provider"
        req.role = api_pb2.RPCRegistrationRequest.STATUS_BAR_COMPONENT
        # The function's signature on the AutoLaunch side accepts
        # (knobs, session_id) — list every kw arg here.
        for arg_name in ("session_id",):
            req.arguments.add().name = arg_name
        sba = req.status_bar_component_attributes
        sba.short_description = "teammate label"
        sba.detailed_description = "Shows the teammate-mcp label registered for this pane."
        sba.exemplar = "[codex1]"
        sba.update_cadence = 2.0
        sba.unique_identifier = "com.teammate.label"
        sba.format = api_pb2.RPCRegistrationRequest.StatusBarComponentAttributes.PLAIN_TEXT
        encoded = _b64.b64encode(req.SerializeToString()).decode("ascii")

        layout_components = [
            {
                "class": "iTermStatusBarRPCProvidedTextComponent",
                "configuration": {
                    "registration request v2": encoded,
                    "knob values": {
                        "base: priority": 5,
                        "base: compression resistance": 1,
                    },
                    "layout advanced configuration dictionary value": {
                        "remove empty components": True,
                        "font": ".AppleSystemUIFont 12",
                        "algorithm": 0,
                    },
                },
            },
        ]

    profile_path = dyn_dir / "teammate.json"
    profile = {
        "Profiles": [
            {
                "Name": "Teammate",
                "Guid": "C9F7E2B4-1A3F-4D89-A0B2-7E5F8C9D1234",
                "Dynamic Profile Parent Name": "Default",
                "Show Status Bar": True,
                "Status Bar Layout": {
                    "components": layout_components,
                    "advanced configuration": {
                        "remove empty components": True,
                        "font": ".AppleSystemUIFont 12",
                        "algorithm": 0,
                        "auto-rainbow style": 0,
                    },
                },
            }
        ]
    }
    profile_path.write_text(_json.dumps(profile, indent=2))
    print(f"✓ Dynamic profile: {profile_path}")
    if layout_components:
        print(f"  • Status bar layout pre-installed with the 'teammate label' RPC component")
    else:
        print(f"  • Status bar enabled but layout NOT pre-installed (protobuf missing)")

    print()
    print("Final steps:")
    print("  1. Restart iTerm2 (so it picks up the AutoLaunch script + the new profile).")
    print("  2. Open a pane with the 'Teammate' profile (or run tmclaude / tmcodex).")
    print("  3. Run /team-register or `teammate-mcp register-pane`.")
    print("  → The label appears at the bottom of the pane automatically.")
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

    if label:
        print(f"[{label}]")
    else:
        print("[unregistered]  run /team-register")
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
    if cmd in ("register-pane", "register", "reg"):
        sys.exit(_cmd_register_pane(rest))
    if cmd == "list":
        sys.exit(_cmd_list())
    if cmd == "prune":
        sys.exit(_cmd_prune())
    if cmd == "whoami":
        sys.exit(_cmd_whoami())
    if cmd == "exists":
        sys.exit(_cmd_exists(rest))
    if cmd == "ask":
        sys.exit(_cmd_ask(rest))
    if cmd == "inbox":
        sys.exit(_cmd_inbox(rest))
    if cmd in ("mark-processed", "ack", "mark"):
        sys.exit(_cmd_mark_processed(rest))
    if cmd == "drain":
        sys.exit(_cmd_drain(rest))
    if cmd in ("watch", "watchdog"):
        from .watcher import main as watcher_main
        sys.exit(watcher_main(rest))
    if cmd == "unregister":
        sys.exit(_cmd_unregister(rest))
    if cmd == "statusline":
        sys.exit(_cmd_statusline())
    if cmd == "install-iterm":
        sys.exit(_cmd_install_iterm())

    print(f"unknown subcommand: {cmd!r}", file=sys.stderr)
    print(HELP, file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
