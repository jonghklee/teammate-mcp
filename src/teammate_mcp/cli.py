"""Console-script entrypoint: `teammate-mcp [serve|register-pane|list|...]`."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
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
                                Default ASYNC / mailbox-only. Set
                                TEAMMATE_INJECT=1 only for legacy
                                best-effort keystroke delivery.
                                For bodies > ~500KB use --stdin or --body-file
                                to avoid OS argv limit (ARG_MAX).
                                  echo "<huge>" | teammate-mcp ask --stdin LBL
                                  teammate-mcp ask --body-file <path> LBL
  teammate-mcp inbox [LBL]      list pending mailbox entries for LBL
                                (defaults to caller's own pane label)
  teammate-mcp next-reply       print the oldest matching reply in LBL's
        [--consume] LBL FROM... inbox/processed as JSON. Matches by
                                from_ or by body prefix "FROM:".
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
        [--interval N]          Claude Code panes by submitting "."
        [--health|--ensure]     check/start the detached watchdog
                                (only when compose box looks empty).
  teammate-mcp unregister LBL   remove a label from the registry
  teammate-mcp status           print queue status as JSON
  teammate-mcp version          print version
  teammate-mcp help             this message

Environment variables:
  TEAMMATE_LABEL        explicit label override for register-pane
  TEAMMATE_QUEUE_MODE   ephemeral|audit  (default: ephemeral)
  TEAMMATE_CWD          override pane disambiguation cwd (default: $PWD)
  TEAMMATE_MCP_MAILBOX_ONLY
                         1|true|yes|on forces mailbox-only `ask`
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


SPAWN_LEDGER_PATH = Path.home() / ".teammate-mcp" / "run" / "spawn_ledger.jsonl"


def _append_spawn_ledger(record: dict) -> None:
    """Append-only spawn ledger. One JSON object per line.

    Used by ``despawn`` to find which panes were created by this tool
    versus ones the user opened by hand. Reading uses ``_load_spawn_ledger``
    which dedupes by label (last write wins).
    """
    SPAWN_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SPAWN_LEDGER_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _load_spawn_ledger() -> dict[str, dict]:
    """Read the ledger, deduping by label (latest entry per label wins).

    Returns a ``{label: record}`` dict. Records whose pane is no longer
    in the registry are still returned — the caller decides whether to
    treat them as live or stale.
    """
    if not SPAWN_LEDGER_PATH.exists():
        return {}
    out: dict[str, dict] = {}
    try:
        with SPAWN_LEDGER_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                label = rec.get("label")
                if isinstance(label, str) and label:
                    out[label] = rec  # last write wins
    except OSError:
        return {}
    return out


def _save_spawn_ledger(records: dict[str, dict]) -> None:
    """Rewrite the ledger from scratch with the given records.

    Used after ``despawn`` so the dropped labels don't keep coming back
    in future ``spawned`` listings.
    """
    SPAWN_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SPAWN_LEDGER_PATH.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for rec in records.values():
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    tmp.replace(SPAWN_LEDGER_PATH)


def _close_iterm_session(session_id: str) -> bool:
    """Close the iTerm session with the given UUID. Returns True iff
    the AppleScript reported a match.

    Two-phase to avoid the "close + then read session properties"
    AppleScript edge case where the closed session object becomes
    invalid mid-iteration:
      1. Walk the session tree, find the target, remember it.
      2. After the walk, close the remembered session and return ok.
    """
    sid_q = session_id.replace('"', '')
    script = (
        'tell application "iTerm"\n'
        '    set targetSess to missing value\n'
        '    repeat with w in windows\n'
        '        repeat with t in tabs of w\n'
        '            repeat with s in sessions of t\n'
        f'                if (unique id of s as string) is "{sid_q}" then\n'
        '                    set targetSess to s\n'
        '                end if\n'
        '            end repeat\n'
        '        end repeat\n'
        '    end repeat\n'
        '    if targetSess is not missing value then\n'
        '        close targetSess\n'
        '        return "ok"\n'
        '    end if\n'
        '    return "not-found"\n'
        'end tell'
    )
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=8,
        )
        return (r.stdout or "").strip() == "ok"
    except Exception:
        return False


_BOOT_PROMPT_TRIGGERS = (
    "trust this folder",
    "trust the contents",
    "trust this project",
    "do you trust",
    "yes, continue",
    "yes, i trust",
    "1. yes",
)


def _auto_dismiss_boot_prompts(sid: str, attempts: int = 3, gap_s: float = 1.5) -> bool:
    """Best-effort: detect claude/codex first-launch trust prompts in the
    given pane and confirm them by sending Enter.

    Some agents show a one-time "trust this folder?" dialog when launched
    in an unfamiliar cwd. The spawn command would otherwise hang at that
    dialog and the MCP server never gets to auto-register the label.

    Returns True if a prompt was detected and confirmed at any attempt,
    False otherwise. Safe to call when no prompt exists — it is a no-op
    in that case.
    """
    sid_q = sid.replace('"', '')
    capture = (
        'tell application "iTerm"\n'
        '    repeat with w in windows\n'
        '        repeat with t in tabs of w\n'
        '            repeat with s in sessions of t\n'
        f'                if (unique id of s as string) is "{sid_q}" then\n'
        '                    return (contents of s)\n'
        '                end if\n'
        '            end repeat\n'
        '        end repeat\n'
        '    end repeat\n'
        'end tell'
    )
    confirm = (
        'tell application "iTerm"\n'
        '    repeat with w in windows\n'
        '        repeat with t in tabs of w\n'
        '            repeat with s in sessions of t\n'
        f'                if (unique id of s as string) is "{sid_q}" then\n'
        '                    tell s to write text ""\n'
        '                end if\n'
        '            end repeat\n'
        '        end repeat\n'
        '    end repeat\n'
        'end tell'
    )

    detected_any = False
    for _ in range(attempts):
        try:
            r = subprocess.run(
                ["osascript", "-e", capture],
                capture_output=True, text=True, timeout=5,
            )
            content_lower = (r.stdout or "").lower()
        except Exception:
            content_lower = ""

        if any(t in content_lower for t in _BOOT_PROMPT_TRIGGERS):
            detected_any = True
            try:
                subprocess.run(
                    ["osascript", "-e", confirm],
                    capture_output=True, timeout=5,
                )
            except Exception:
                pass
            time.sleep(gap_s)
        else:
            break
    return detected_any


def _resolve_screen_region(name: str) -> Optional[tuple[int, int, int, int]]:
    """Translate a named region (``left``/``right``/``top``/``bottom``/``full``)
    into a pixel rect ``(x1, y1, x2, y2)`` on the main display.

    Uses AppleScript to read the main screen's frame so the resolution
    is correct on retina, scaled, or external monitor setups. Returns
    None on unrecognized names; the caller treats that as a usage error.

    On a 1920×1080 display:
        left   → ( 0,    0,    960,  1080)
        right  → ( 960,  0,    1920, 1080)
        top    → ( 0,    0,    1920, 540)
        bottom → ( 0,    540,  1920, 1080)
        full   → ( 0,    0,    1920, 1080)
    """
    name = name.strip().lower()
    if name not in ("left", "right", "top", "bottom", "full"):
        return None

    # Read main screen bounds via AppleScript. "desktop" of Finder gives
    # the main screen rect. Format: {x1, y1, x2, y2}.
    try:
        r = subprocess.run(
            ["osascript", "-e",
             'tell application "Finder" to get bounds of window of desktop'],
            capture_output=True, text=True, timeout=4,
        )
        # Output is like: "0, 0, 1920, 1080"
        parts = [int(p.strip()) for p in r.stdout.strip().split(",")]
        if len(parts) != 4:
            return None
        sx1, sy1, sx2, sy2 = parts
    except Exception:
        # Fallback to common-ish default if AppleScript fails.
        sx1, sy1, sx2, sy2 = 0, 0, 1920, 1080

    width = sx2 - sx1
    height = sy2 - sy1
    if name == "left":
        return (sx1, sy1, sx1 + width // 2, sy2)
    if name == "right":
        return (sx1 + width // 2, sy1, sx2, sy2)
    if name == "top":
        return (sx1, sy1, sx2, sy1 + height // 2)
    if name == "bottom":
        return (sx1, sy1 + height // 2, sx2, sy2)
    # full
    return (sx1, sy1, sx2, sy2)


def _normalize_term_session_id(raw: str) -> str:
    raw = (raw or "").strip()
    if ":" in raw:
        return raw.split(":", 1)[1].strip()
    return raw


def _resolve_caller_session_id() -> str:
    """Resolve the iTerm session UUID of the *caller's* pane — the pane
    where this task/agent is actually running — NOT iTerm's currently
    focused pane.

    Priority:
      1. ``TEAMMATE_LABEL`` env → registry lookup → session_id. Most
         reliable: a registered agent (e.g. ``claude3``) maps to its real
         pane even when ``TERM_SESSION_ID`` is unreliable. (Claude Code's
         bash subprocesses can carry a different ``TERM_SESSION_ID`` than
         the pane that launched the long-lived MCP server.)
      2. ``TERM_SESSION_ID`` env, normalized.

    Returns an uppercase session UUID, or "" if nothing is resolvable —
    in which case the caller must NOT split a focused pane (open a window
    instead).
    """
    from . import registry as _reg
    label = os.environ.get("TEAMMATE_LABEL", "").strip()
    if label:
        rec = _reg.lookup(label)
        if rec and rec.get("session_id"):
            return _normalize_term_session_id(rec["session_id"]).upper()
    sid = _normalize_term_session_id(os.environ.get("TERM_SESSION_ID", ""))
    return sid.upper()


def _anchor_session_script_prefix(anchor_session_id: str) -> str:
    sid = anchor_session_id.replace("\\", "\\\\").replace('"', '\\"').upper()
    return (
        '    set anchorSession to missing value\n'
        '    set anchorWindow to missing value\n'
        '    repeat with w in windows\n'
        '        repeat with t in tabs of w\n'
        '            repeat with s in sessions of t\n'
        f'                if ((unique id of s) as string) is "{sid}" then\n'
        '                    set anchorSession to s\n'
        '                    set anchorWindow to w\n'
        '                    exit repeat\n'
        '                end if\n'
        '            end repeat\n'
        '            if anchorSession is not missing value then exit repeat\n'
        '        end repeat\n'
        '        if anchorSession is not missing value then exit repeat\n'
        '    end repeat\n'
        '    if anchorSession is missing value then error "anchor session not found"\n'
    )


def _build_spawn_applescript(
    mode: str,
    shell_line: str,
    bounds: Optional[tuple[int, int, int, int]] = None,
    anchor_session_id: str = "",
) -> str:
    """Return an AppleScript that creates a new iTerm session in the
    requested layout, types ``shell_line`` into it, and returns the new
    session's UUID on stdout.

    ``shell_line`` is the literal text typed into the new pane after a
    fresh shell prompt — usually
    ``export TEAMMATE_LABEL=foo && cd /path && claude``.
    """
    safe = shell_line.replace("\\", "\\\\").replace('"', '\\"')

    if mode == "window":
        bounds_line = ""
        if bounds is not None:
            x1, y1, x2, y2 = bounds
            bounds_line = f'    set bounds of newWin to {{{x1}, {y1}, {x2}, {y2}}}\n'
        return (
            'tell application "iTerm"\n'
            '    activate\n'
            '    set newWin to (create window with default profile)\n'
            + bounds_line +
            '    tell current session of newWin\n'
            f'        write text "{safe}"\n'
            '        return (unique id) as string\n'
            '    end tell\n'
            'end tell'
        )
    if mode == "tab":
        if anchor_session_id:
            return (
                'tell application "iTerm"\n'
                + _anchor_session_script_prefix(anchor_session_id) +
                '    tell anchorWindow\n'
                '        set newTab to (create tab with default profile)\n'
                '        tell current session of newTab\n'
                f'            write text "{safe}"\n'
                '            return (unique id) as string\n'
                '        end tell\n'
                '    end tell\n'
                'end tell'
            )
        return (
            'tell application "iTerm"\n'
            '    activate\n'
            '    tell current window\n'
            '        set newTab to (create tab with default profile)\n'
            '        tell current session of newTab\n'
            f'            write text "{safe}"\n'
            '            return (unique id) as string\n'
            '        end tell\n'
            '    end tell\n'
            'end tell'
        )
    if mode in ("split-v", "split"):
        if anchor_session_id:
            return (
                'tell application "iTerm"\n'
                + _anchor_session_script_prefix(anchor_session_id) +
                '    tell anchorSession\n'
                '        set newSess to (split vertically with default profile)\n'
                '        tell newSess\n'
                f'            write text "{safe}"\n'
                '            return (unique id) as string\n'
                '        end tell\n'
                '    end tell\n'
                'end tell'
            )
        return (
            'tell application "iTerm"\n'
            '    activate\n'
            '    tell current session of current window\n'
            '        set newSess to (split vertically with default profile)\n'
            '        tell newSess\n'
            f'            write text "{safe}"\n'
            '            return (unique id) as string\n'
            '        end tell\n'
            '    end tell\n'
            'end tell'
        )
    if mode == "split-h":
        if anchor_session_id:
            return (
                'tell application "iTerm"\n'
                + _anchor_session_script_prefix(anchor_session_id) +
                '    tell anchorSession\n'
                '        set newSess to (split horizontally with default profile)\n'
                '        tell newSess\n'
                f'            write text "{safe}"\n'
                '            return (unique id) as string\n'
                '        end tell\n'
                '    end tell\n'
                'end tell'
            )
        return (
            'tell application "iTerm"\n'
            '    activate\n'
            '    tell current session of current window\n'
            '        set newSess to (split horizontally with default profile)\n'
            '        tell newSess\n'
            f'            write text "{safe}"\n'
            '            return (unique id) as string\n'
            '        end tell\n'
            '    end tell\n'
            'end tell'
        )
    raise ValueError(
        f"unknown mode {mode!r}; choose one of: window, tab, split, split-v, split-h"
    )


def _cmd_spawn(argv: list[str]) -> int:
    """Spawn a new iTerm pane with a label, then optionally send an
    initial message to it.

    Usage::

        teammate-mcp spawn <label> <command...>  [options]

    Options::

        -m, --message "<msg>"   send this as the first ask after the
                                pane is registered.
        --cwd PATH              working directory for the new pane
                                (default: current cwd)
        --mode <m>              layout: ``split-v`` (default), ``split-h``,
                                ``tab``, ``window``. The default splits the
                                CALLER's pane (the pane this task runs in);
                                if that pane can't be resolved it falls back
                                to ``window``.
        --anchor LBL_OR_SID     for tab/split modes, create next to the
                                given label/session. Defaults to the
                                caller's own pane (TEAMMATE_LABEL→registry,
                                then TERM_SESSION_ID) — never the focused
                                pane.
        --wait-s N              max seconds to wait for the new pane to
                                register itself (default 20)
        --no-wait               don't wait for registration
        --yolo                  if the command is ``codex``, automatically
                                append ``--yolo`` so it bypasses approval
                                + sandbox (matches the ``tmcodex`` alias
                                behavior). No effect on non-codex commands.
        --bounds x1,y1,x2,y2    pixel rect for the new window (only when
                                ``--mode window``). Top-left + bottom-right.
                                Example: ``--bounds 100,100,1300,900``
        --screen <region>       semantic position for a new window:
                                ``left``  — left half of main screen
                                ``right`` — right half
                                ``top``   — top half
                                ``bottom``— bottom half
                                ``full``  — full screen
                                Overrides ``--bounds`` if both given.

    Examples::

        teammate-mcp spawn w1 codex --yolo --screen right
        teammate-mcp spawn coder codex --yolo --cwd ~/api --mode window
        teammate-mcp spawn helper claude --bounds 100,100,1200,800
        teammate-mcp spawn r1 claude --screen left -m "이거 분석해줘"
    """
    message = ""
    cwd_arg = ""
    # Default layout: split the CALLER's pane (the pane where this task is
    # running), resolved below via _resolve_caller_session_id(). Falls
    # back to a new window only if the caller pane can't be identified.
    mode = "split-v"
    mode_explicit = False
    wait_s = 20
    no_wait = False
    yolo = False
    bounds: Optional[tuple[int, int, int, int]] = None
    screen_region = ""
    anchor_arg = ""

    args: list[str] = []
    src = list(argv)
    while src:
        a = src.pop(0)
        if a in ("-m", "--message"):
            if src:
                message = src.pop(0)
        elif a.startswith("--message="):
            message = a.split("=", 1)[1]
        elif a == "--cwd":
            if src:
                cwd_arg = src.pop(0)
        elif a.startswith("--cwd="):
            cwd_arg = a.split("=", 1)[1]
        elif a == "--mode":
            if src:
                mode = src.pop(0)
                mode_explicit = True
        elif a.startswith("--mode="):
            mode = a.split("=", 1)[1]
            mode_explicit = True
        elif a == "--anchor":
            if src:
                anchor_arg = src.pop(0)
        elif a.startswith("--anchor="):
            anchor_arg = a.split("=", 1)[1]
        elif a == "--wait-s":
            if src:
                try:
                    wait_s = int(src.pop(0))
                except ValueError:
                    print("ERROR: --wait-s must be an integer", file=sys.stderr)
                    return 2
        elif a == "--no-wait":
            no_wait = True
        elif a == "--yolo":
            yolo = True
        elif a == "--bounds":
            if src:
                try:
                    parts = [int(p.strip()) for p in src.pop(0).split(",")]
                    if len(parts) != 4:
                        raise ValueError("need 4 ints")
                    bounds = (parts[0], parts[1], parts[2], parts[3])
                except ValueError as e:
                    print(f"ERROR: --bounds expects 'x1,y1,x2,y2' ({e})",
                          file=sys.stderr)
                    return 2
        elif a.startswith("--bounds="):
            try:
                parts = [int(p.strip()) for p in a.split("=", 1)[1].split(",")]
                if len(parts) != 4:
                    raise ValueError("need 4 ints")
                bounds = (parts[0], parts[1], parts[2], parts[3])
            except ValueError as e:
                print(f"ERROR: --bounds expects 'x1,y1,x2,y2' ({e})",
                      file=sys.stderr)
                return 2
        elif a == "--screen":
            if src:
                screen_region = src.pop(0).lower()
        elif a.startswith("--screen="):
            screen_region = a.split("=", 1)[1].lower()
        elif a == "--":
            args.extend(src)
            break
        else:
            args.append(a)

    if len(args) < 2:
        print(
            "usage: teammate-mcp spawn <label> <command...> "
            "[-m MSG] [--cwd PATH] [--mode ...] [--anchor LBL_OR_SID] "
            "[--wait-s N] [--no-wait] "
            "[--yolo] [--bounds x1,y1,x2,y2] [--screen left|right|top|bottom|full]",
            file=sys.stderr,
        )
        return 2

    label = args[0].strip()
    if not label or any(c in label for c in (" ", "\t", "\n", '"', "'")):
        print(f"ERROR: invalid label {label!r}", file=sys.stderr)
        return 2

    command = " ".join(args[1:]).strip()
    if not command:
        print("ERROR: empty command", file=sys.stderr)
        return 2

    # --yolo: if the command starts with `codex` (and doesn't already
    # have --yolo / --dangerously-bypass), append --yolo to match the
    # `tmcodex` alias behavior. No effect on claude/other commands.
    if yolo:
        first_word = command.split()[0] if command.split() else ""
        if first_word == "codex" and "--yolo" not in command and \
                "--dangerously-bypass" not in command:
            command = command + " --yolo"
    first_word = command.split()[0] if command.split() else ""
    repo_root = Path(__file__).resolve().parent.parent.parent
    launch_command = command
    if first_word == "codex":
        launch_command = f'"{repo_root / "bin" / "tmcodex"}"'
    elif first_word == "claude":
        launch_command = f'"{repo_root / "bin" / "tmclaude"}"'

    # --screen <region> overrides --bounds. Resolve the named region into
    # pixel coords using the main display's frame.
    if screen_region:
        bounds = _resolve_screen_region(screen_region)
        if bounds is None:
            print(
                f"ERROR: --screen must be one of: left, right, top, bottom, full "
                f"(got {screen_region!r})",
                file=sys.stderr,
            )
            return 2

    if bounds is not None and mode != "window":
        if mode_explicit:
            # User explicitly asked for a split/tab AND a window rect —
            # contradictory. Respect the explicit --mode, drop the rect.
            print(f"⚠ --bounds/--screen only takes effect with --mode window "
                  f"(explicit --mode {mode}); ignoring", file=sys.stderr)
            bounds = None
        else:
            # --bounds/--screen is an explicit "I want a positioned window"
            # signal; it overrides the default split layout.
            mode = "window"

    anchor_session_id = ""
    if mode in ("tab", "split", "split-v", "split-h"):
        from . import registry as _reg
        if anchor_arg:
            rec = _reg.lookup(anchor_arg)
            if rec and rec.get("session_id"):
                anchor_session_id = _normalize_term_session_id(rec["session_id"]).upper()
            else:
                anchor_session_id = _normalize_term_session_id(anchor_arg).upper()
        else:
            # No explicit --anchor: split the CALLER's own pane, not the
            # focused pane.
            anchor_session_id = _resolve_caller_session_id()
        if not anchor_session_id:
            # Could not identify the caller's pane. Do NOT fall back to
            # splitting iTerm's *focused* session (whatever the user
            # happens to be looking at) — open a non-destructive new
            # window instead.
            print("⚠ could not resolve caller pane for split; "
                  "falling back to --mode window", file=sys.stderr)
            mode = "window"

    # cwd resolution + existence check
    cwd_path = Path(cwd_arg).expanduser() if cwd_arg else Path.cwd()
    cwd_abs = cwd_path.resolve()
    if not cwd_abs.is_dir():
        print(f"ERROR: cwd does not exist or is not a directory: {cwd_abs}",
              file=sys.stderr)
        return 2

    # Shell line that the new pane will execute. We base64-encode it
    # and run it via ``eval "$(... | base64 -d)"`` because raw text
    # injected through AppleScript's ``write text`` passes through the
    # macOS keyboard layer, which means an active non-ASCII IME (Korean,
    # Japanese, etc.) can corrupt the very first characters of the
    # command. Base64 output is purely ASCII alphanumeric + ``/+=``, so
    # no IME treats it as composable input. The decoded payload preserves
    # quoting, multibyte characters in the inner command, etc.
    import base64 as _b64
    cwd_q = str(cwd_abs).replace('"', '\\"')
    inner = (
        f'export TEAMMATE_LABEL="{label}" && '
        f'cd "{cwd_q}" && '
        f'{launch_command}'
    )
    inner_b64 = _b64.b64encode(inner.encode("utf-8")).decode("ascii")
    shell_line = f'eval "$(echo {inner_b64} | base64 -d)"'

    try:
        applescript = _build_spawn_applescript(
            mode,
            shell_line,
            bounds=bounds,
            anchor_session_id=anchor_session_id,
        )
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    try:
        result = subprocess.run(
            ["osascript", "-e", applescript],
            check=True, capture_output=True, text=True, timeout=15,
        )
    except subprocess.CalledProcessError as e:
        print("ERROR: osascript failed:", file=sys.stderr)
        print(e.stderr or e.stdout or "(no output)", file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired:
        print("ERROR: osascript timed out — iTerm unresponsive?",
              file=sys.stderr)
        return 1

    new_sid = (result.stdout or "").strip()
    print(f"✓ spawned: label={label} sid={new_sid[:8] if new_sid else '?'} "
          f"cwd={cwd_abs} mode={mode}")

    # Record this spawn so later `despawn` calls can target it. We write
    # to a sidecar ledger immediately (the registry entry from
    # auto_register lands a couple seconds later via the new pane's MCP
    # server, and we can't rely on having raced ahead of that). The
    # ledger has at-least-once semantics — `despawn --all` dedupes by
    # label.
    try:
        _append_spawn_ledger({
            "label": label,
            "session_id": new_sid,
            "cwd": str(cwd_abs),
            "command": command,
            "mode": mode,
            "spawned_at": time.time(),
            "spawner_pid": os.getpid(),
        })
    except Exception:
        pass  # ledger is best-effort; not having it just means `despawn` falls back to registry lookup

    if no_wait:
        if message:
            print("⚠ --no-wait set but -m given; message NOT sent",
                  file=sys.stderr)
        return 0

    # First-launch trust prompts (Claude/Codex) would otherwise hold the
    # pane and block MCP startup → no auto-register → spawn looks broken.
    # Best-effort auto-confirm. No-op if no prompt is shown.
    if new_sid:
        time.sleep(2.5)
        if _auto_dismiss_boot_prompts(new_sid):
            print("  auto-confirmed first-launch trust prompt",
                  file=sys.stderr)

    # Wait for label to appear in the registry (auto_register fires
    # when the new pane's MCP server starts).
    from . import registry as _reg
    deadline = time.monotonic() + wait_s
    print(f"  waiting for registration (max {wait_s}s)...", file=sys.stderr)
    registered = False
    while time.monotonic() < deadline:
        if _reg.lookup(label):
            registered = True
            break
        time.sleep(0.5)

    if not registered:
        print(f"⚠ label '{label}' did not register within {wait_s}s.",
              file=sys.stderr)
        print(f"  The pane may still be booting; check `teammate-mcp list` "
              f"in a moment.", file=sys.stderr)
        if message:
            print(f"  Skipping initial message — sender has no destination.",
                  file=sys.stderr)
        return 0

    print(f"✓ registered: {label}", file=sys.stderr)

    if message:
        # Brief grace so the TUI has a compose box ready.
        time.sleep(2.0)
        print(f"  sending initial message ({len(message)} chars)...",
              file=sys.stderr)
        from .server import _ask_async
        try:
            answer = asyncio.run(_ask_async(question=message, target=label))
            print(answer)
        except Exception as e:
            print(f"ERROR sending message: {e}", file=sys.stderr)
            return 1

    return 0


def _cmd_close_pane(argv: list[str]) -> int:
    """Close an iTerm pane by label OR session_id, regardless of how it
    was created.

    Unlike ``despawn``, this does NOT require the pane to be in the spawn
    ledger. Use it from cleanup scripts (``.claude/skills/.../cleanup_pane.sh``)
    or any place that knows the target identifier.

    Usage::
        teammate-mcp close-pane <label>
        teammate-mcp close-pane <session-id>     # 8-char prefix OK
        teammate-mcp close-pane <label> --keep-label   # close pane, keep registry entry

    Default behavior: close the pane AND remove the registry entry (and
    the ledger entry if it existed). Pass ``--keep-label`` to close only.
    """
    keep_label = "--keep-label" in argv
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 1:
        print("usage: teammate-mcp close-pane <label-or-sid> [--keep-label]",
              file=sys.stderr)
        return 2
    target = args[0]

    from . import registry as _reg, daemon_client

    # Resolve label → sid. If user passed an explicit sid (UUID-ish, len ≥ 8
    # and contains hex+dashes only), use it directly.
    sid = ""
    label_to_drop = ""
    rec = _reg.lookup(target)
    if rec:
        sid = (rec.get("session_id") or "").strip()
        label_to_drop = target
    else:
        # Try sid match (prefix)
        target_up = target.upper().strip()
        for lbl, r in _reg.all_labels().items():
            rs = (r.get("session_id") or "").upper()
            if rs == target_up or rs.startswith(target_up) or target_up.startswith(rs):
                sid = r.get("session_id") or ""
                label_to_drop = lbl
                break
        if not sid:
            # Assume user passed a raw sid not in the registry — still
            # try to close.
            if len(target_up) >= 8 and all(c in "ABCDEF0123456789-" for c in target_up):
                sid = target

    if not sid:
        print(f"ERROR: no pane matches {target!r} "
              f"(not a label nor recognizable session_id)",
              file=sys.stderr)
        return 2

    ok = _close_iterm_session(sid)
    if ok:
        print(f"✓ closed pane sid={sid[:8]}")
    else:
        print(f"⚠ AppleScript reported no match for sid={sid[:8]} "
              f"— pane may already be closed", file=sys.stderr)

    if not keep_label and label_to_drop:
        if daemon_client.is_enabled():
            daemon_client.unregister(label_to_drop)
        else:
            try:
                _reg.unregister(label_to_drop)
            except Exception:
                pass
        # If it was in the ledger, drop that too.
        ledger = _load_spawn_ledger()
        if label_to_drop in ledger:
            ledger.pop(label_to_drop, None)
            _save_spawn_ledger(ledger)
        print(f"✓ dropped label '{label_to_drop}' from registry/ledger",
              file=sys.stderr)

    return 0 if ok else 1


def _cmd_spawned(argv: list[str]) -> int:
    """List panes this tool spawned (recorded in the spawn ledger).

    Usage::
        teammate-mcp spawned          # human table
        teammate-mcp spawned --json   # JSON output
    """
    as_json = "--json" in argv
    ledger = _load_spawn_ledger()

    from . import registry as _reg
    live = _reg.all_labels()

    rows: list[dict] = []
    for label, rec in sorted(ledger.items()):
        sid = (rec.get("session_id") or "").upper()
        in_reg = label in live
        reg_sid = (live.get(label, {}).get("session_id") or "").upper()
        sid_match = bool(sid) and sid == reg_sid
        status = "alive" if (in_reg and sid_match) else (
            "label-reused" if in_reg else "gone"
        )
        rows.append({
            "label": label,
            "session_id": rec.get("session_id"),
            "cwd": rec.get("cwd"),
            "command": rec.get("command"),
            "mode": rec.get("mode"),
            "spawned_at": rec.get("spawned_at"),
            "status": status,
        })

    if as_json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0

    if not rows:
        print("(no spawned panes recorded)")
        return 0

    import datetime as _dt
    print(f"{'STATUS':<13} {'MODE':<10} {'LABEL':<25} {'CWD':<35} CMD")
    print("─" * 110)
    for r in rows:
        ts = r.get("spawned_at") or 0
        when = _dt.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M") if ts else "?"
        print(f"{r['status']:<13} {(r.get('mode') or '?'):<10} "
              f"{r['label']:<25} {(r.get('cwd') or '')[:33]:<35} "
              f"{(r.get('command') or '')[:35]}    (spawned {when})")
    return 0


def _cmd_despawn(argv: list[str]) -> int:
    """Close (and unregister) panes this tool spawned.

    Usage::
        teammate-mcp despawn <label>
        teammate-mcp despawn --all                # close every spawned pane
        teammate-mcp despawn --gone               # purge ledger entries
                                                  # whose pane is already gone

    Refuses to close panes that were NOT spawned by this tool (they
    won't be in the ledger). Use ``unregister`` to drop a label without
    touching the pane.
    """
    do_all = "--all" in argv
    do_gone = "--gone" in argv
    labels = [a for a in argv if not a.startswith("--")]

    ledger = _load_spawn_ledger()
    if not ledger:
        print("(spawn ledger is empty — nothing to despawn)")
        return 0

    targets: list[str]
    if do_all:
        targets = list(ledger.keys())
    elif do_gone:
        from . import registry as _reg
        live = _reg.all_labels()
        targets = []
        for label, rec in ledger.items():
            sid = (rec.get("session_id") or "").upper()
            reg_sid = (live.get(label, {}).get("session_id") or "").upper()
            if not (label in live and sid == reg_sid):
                targets.append(label)
    elif labels:
        targets = labels
    else:
        print("usage: teammate-mcp despawn <label> | --all | --gone",
              file=sys.stderr)
        return 2

    from . import registry as _reg, daemon_client
    live = _reg.all_labels()

    closed = 0
    not_found = 0
    skipped = 0
    purged = 0
    for label in targets:
        rec = ledger.get(label)
        if rec is None:
            print(f"  skip {label}: not in spawn ledger "
                  f"(use `teammate-mcp unregister {label}` if you only "
                  f"want to drop the label)", file=sys.stderr)
            skipped += 1
            continue
        sid = rec.get("session_id") or ""
        reg_sid = (live.get(label, {}).get("session_id") or "").upper()
        ledger_sid = sid.upper()
        if reg_sid and ledger_sid and reg_sid != ledger_sid:
            if do_gone:
                print(
                    f"  ✓ purged stale ledger: {label} "
                    f"(ledger sid={sid[:8]}, registry sid={reg_sid[:8]})"
                )
                ledger.pop(label, None)
                purged += 1
                continue
            print(
                f"  skip {label}: label reused "
                f"(ledger sid={sid[:8]}, registry sid={reg_sid[:8]})",
                file=sys.stderr,
            )
            skipped += 1
            continue
        ok = _close_iterm_session(sid) if sid else False
        if ok:
            closed += 1
            print(f"  ✓ closed pane: {label} (sid={sid[:8]})")
        else:
            not_found += 1
            print(f"  ⚠ pane not found: {label} (sid={sid[:8]}) "
                  f"— may already be closed")

        # Drop from registry too. Prefer daemon, fall back to direct.
        current_sid = (live.get(label, {}).get("session_id") or "").upper()
        if current_sid and ledger_sid and current_sid != ledger_sid:
            print(
                f"  skip unregister {label}: registry now points at "
                f"{current_sid[:8]}, not spawned sid {sid[:8]}",
                file=sys.stderr,
            )
        elif daemon_client.is_enabled():
            daemon_client.unregister(label)
        else:
            try:
                _reg.unregister(label)
            except Exception:
                pass

        # Remove from ledger
        ledger.pop(label, None)

    _save_spawn_ledger(ledger)

    print(
        f"\nsummary: closed={closed} not_found={not_found} "
        f"skipped={skipped} purged={purged}",
          file=sys.stderr)
    return 0 if (closed + not_found + purged > 0) else 1


def _register_through_daemon_or_file(
    label: str,
    session_id: str,
    pid: int,
    job: str,
    cwd: Optional[str] = None,
    extra: Optional[dict] = None,
) -> None:
    """Register a label, preferring the daemon when available.

    Routes to daemon RPC when ``TEAMMATE_DAEMON=1`` and the socket is
    reachable. Falls back to direct file-based ``registry.register``
    transparently if anything goes wrong, so a missing/crashing daemon
    never blocks a register call.
    """
    from . import daemon_client, registry as _reg

    if daemon_client.is_enabled():
        result = daemon_client.register(
            label=label,
            session_id=session_id,
            pid=pid,
            job=job,
            cwd=cwd,
            extra=extra or {},
        )
        if result is not None:
            return

    # Fallback: direct file write (legacy path).
    _reg.register(
        label=label,
        session_id=session_id,
        pid=pid,
        job=job,
        cwd=cwd,
        extra=extra or {},
        dedupe_session_id=True,
    )


def _emit_badge_osc(label: str) -> None:
    """Set iTerm Badge (top-right watermark) for the current pane.

    Tries three channels in order; whichever one reaches iTerm's escape
    parser first wins. Subsequent writes are still attempted as belt-
    and-suspenders (they're no-op if the badge is already set).

      1. ``/dev/tty`` — controlling terminal. Bypasses stdout buffering
         AND zsh prompt rendering. BEST when available.
      2. stdout — works when running from a real shell.
      3. stderr — works in tools that capture stdout (claude bash
         tool, some test runners). stderr usually still goes to the
         pane's tty.

    Why all three: when register-pane is invoked from inside an LLM's
    bash tool, the bash subprocess often loses access to ``/dev/tty``
    (returns ``device not configured`` on macOS) AND has stdout piped
    away. stderr is the last hope, and it usually works.

    Color is intentionally NOT set via OSC — iTerm's OSC 1337
    ``SetColors`` does not accept a ``badge`` key (the supported keys
    are ``fg``, ``bg``, ``bold``, ``link``, ``tab``, ANSI palette, etc.).
    An unrecognized key can cause iTerm to discard the *entire* OSC
    1337 batch, which means SetBadgeFormat right above it never gets
    applied — that was the bug behind "badge isn't showing up after
    register". So we only emit SetBadgeFormat + SetUserVar; the text
    color is whatever iTerm's profile defines (Settings → Profiles →
    Colors → Badge text), and the user can dial alpha there.
    """
    try:
        import base64 as _b64
        badge_b64 = _b64.b64encode(label.encode()).decode()
    except Exception:
        return
    esc = (
        f"\x1b]1337;SetBadgeFormat={badge_b64}\x07"
        f"\x1b]1337;SetUserVar=teammate_label={badge_b64}\x07"
    )

    # Channel 1: /dev/tty (best, bypasses buffering)
    try:
        with open("/dev/tty", "w", encoding="utf-8") as tty_out:
            tty_out.write(esc)
            tty_out.flush()
    except OSError:
        # No controlling tty (e.g. inside an LLM's bash tool); fall through.
        pass

    # Channel 2: stdout — harmless duplicate when /dev/tty also worked.
    try:
        sys.stdout.write(esc)
        sys.stdout.flush()
    except Exception:
        pass

    # Channel 3: stderr — last resort. iTerm parses any byte stream
    # connected to the pty, and stderr is rarely redirected away.
    try:
        sys.stderr.write(esc)
        sys.stderr.flush()
    except Exception:
        pass


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
        _register_through_daemon_or_file(
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
        # Display policy: badge ONLY (top-right watermark), no tab title
        # and no inline banner. Badge text color forced to very low alpha.
        # Write to /dev/tty directly — bypasses stdout buffering and zsh
        # prompt rendering, so the OSC always lands as an escape sequence
        # rather than as literal `^[]1337;...` text in the scrollback.
        # This works whether we are run interactively from a plain shell,
        # piped from claude's bash tool, or invoked via osascript.
        _emit_badge_osc(label)
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

            _register_through_daemon_or_file(
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

            # Set the iTerm badge. Primary path: push OSC bytes through
            # the iterm2 websocket via Session.async_inject, which writes
            # them into the pane's pty as if a program produced them.
            # This bypasses the caller's stdout/stderr/tty entirely, so
            # the badge lands in real time even when register-pane is
            # invoked from inside an LLM bash tool whose stdout is piped
            # away and whose /dev/tty returns "device not configured".
            # The 3-channel _emit_badge_osc remains as a fallback for
            # plain-shell callers and for the (rare) case async_inject
            # fails — in those callers /dev/tty IS available.
            try:
                import base64 as _b64
                _bb = _b64.b64encode(label.encode()).decode()
                _esc = (
                    f"\x1b]1337;SetBadgeFormat={_bb}\x07"
                    f"\x1b]1337;SetUserVar=teammate_label={_bb}\x07"
                ).encode()
                await me.session.async_inject(_esc)
            except Exception:
                _emit_badge_osc(label)

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

    Always async (mailbox-based, "email" mode). The message is persisted
    to ``~/.teammate-mcp/mailbox/<label>/inbox/`` and returns immediately.
    The target's UserPromptSubmit hook drains the inbox on its next user
    prompt; the target replies via reverse async ask.

    Usage:
        teammate-mcp ask <label> <question...>
        teammate-mcp ask --stdin <label>            # body from stdin
        teammate-mcp ask --body-file PATH <label>   # body from file
        teammate-mcp ask --mailbox-only <label> <question...>

    Deprecated flags (accepted silently for backwards compat, no effect):
        --wait, --no-wait, --async, --timeout, -t

    Sync mode was removed — the receiver was always going to reply via
    the same mailbox path; sync just blocked the sender process while
    polling the receiver's screen, which corrupted mid-compose text and
    deadlocked on busy receivers. Use async unconditionally.
    """
    body_from_stdin = False
    body_from_file = None
    inject_requested = os.environ.get("TEAMMATE_INJECT", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    mailbox_only = not inject_requested
    if os.environ.get("TEAMMATE_MCP_MAILBOX_ONLY", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        mailbox_only = True
    # Two-pass: extract flags from anywhere in argv, leaving positional
    # args (label + question words) intact. Deprecated sync flags are
    # silently consumed so old callers don't error out.
    args = []
    src = list(argv)
    while src:
        a = src.pop(0)
        if a in ("--timeout", "-t"):
            # Deprecated — consume the value too.
            if src and not src[0].startswith("--"):
                src.pop(0)
        elif a in ("--no-wait", "--async", "--wait"):
            pass  # deprecated, ignored
        elif a == "--mailbox-only":
            mailbox_only = True
        elif a.startswith("--timeout="):
            pass  # deprecated, ignored
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
            print("usage: teammate-mcp ask [--stdin] [--body-file PATH] <label> <question...>",
                  file=sys.stderr)
            return 2
        target = args[0]
        question = " ".join(args[1:]).strip()
        if not question:
            print("ERROR: empty question", file=sys.stderr)
            return 2

    from .server import _ask_async
    answer = asyncio.run(_ask_async(
        question=question,
        target=target,
        mailbox_only=mailbox_only,
    ))
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


def _cmd_next_reply(argv: list[str]) -> int:
    """Print the oldest matching reply for a mailbox label.

    Usage:
        teammate-mcp next-reply [--consume] <mailbox-label> <sender>...

    This is intentionally Python-based so test harnesses do not have to
    combine shell globs, JSON parsing, and label matching in ad hoc loops.
    """
    consume = False
    args = []
    for a in argv:
        if a == "--consume":
            consume = True
        else:
            args.append(a)
    if len(args) < 2:
        print("usage: teammate-mcp next-reply [--consume] <mailbox-label> <sender>...",
              file=sys.stderr)
        return 2

    mailbox_label = args[0]
    wanted = args[1:]
    wanted_set = set(wanted)

    from . import server as _server

    root = _server.MAILBOX_ROOT / mailbox_label
    candidates = []
    for subdir in ("inbox", "processed"):
        box = root / subdir
        if not box.exists():
            continue
        for path in sorted(box.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            sender = str(data.get("from_", "") or "")
            body = str(data.get("body", "") or "").strip()
            matched = sender if sender in wanted_set else ""
            if not matched:
                for label in wanted:
                    if body.startswith(f"{label}:"):
                        matched = label
                        break
            if not matched:
                continue
            created_at = str(data.get("created_at", "") or "")
            candidates.append((created_at, path.name, path, subdir, matched, data))

    if not candidates:
        print("(no matching reply)")
        return 1

    candidates.sort(key=lambda item: (item[0], item[1]))
    _created_at, _name, path, subdir, matched, data = candidates[0]
    out = {
        "matched_label": matched,
        "mailbox": mailbox_label,
        "subdir": subdir,
        "path": str(path),
        "job_id": data.get("job_id", ""),
        "from_": data.get("from_", ""),
        "body": data.get("body", ""),
        "created_at": data.get("created_at", ""),
    }
    print(json.dumps(out, ensure_ascii=False))
    if consume:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
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


def _cmd_codex_notify(argv: list[str]) -> int:
    """Receive Codex CLI's notify hook payload.

    Wired by adding to ``~/.codex/config.toml``::

        notify = ["teammate-mcp", "codex-notify"]

    Codex passes a JSON blob as the first argv after the program name, or
    on stdin. We append every event to ``~/.teammate-mcp/codex-events.jsonl``
    so watchdog / external monitors can read turn-start / turn-complete
    signals without screen-scraping the pane.

    The hook MUST NOT block — codex waits for it to exit before completing
    the turn. We exit 0 unconditionally, log on best-effort.
    """
    import json as _json
    from datetime import datetime, timezone

    payload_raw = ""
    # Codex sometimes inlines JSON as a single argv item.
    if argv:
        joined = " ".join(argv).strip()
        if joined.startswith("{"):
            payload_raw = joined
    if not payload_raw and not sys.stdin.isatty():
        try:
            payload_raw = sys.stdin.read()
        except Exception:
            payload_raw = ""

    if not payload_raw.strip():
        return 0

    try:
        data = _json.loads(payload_raw)
    except _json.JSONDecodeError:
        return 0

    event = data.get("type") or data.get("event") or "unknown"
    session_id = (
        data.get("session-id") or data.get("session_id")
        or data.get("thread-id") or data.get("thread_id") or ""
    )

    out = Path.home() / ".teammate-mcp" / "codex-events.jsonl"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a", encoding="utf-8") as f:
            f.write(_json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "event": event,
                "session_id": session_id,
                "raw": data,
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return 0


def _cmd_claude_notify(argv: list[str]) -> int:
    """Receive Claude Code's Stop / Notification hook payload.

    Wired by adding to ``~/.claude/settings.json``::

        "Stop": [{"matcher": "", "hooks": [
            {"type": "command", "command": "teammate-mcp claude-notify"}]}]

    Claude passes a JSON event on stdin. We append to
    ``~/.teammate-mcp/claude-events.jsonl``.

    MUST NOT block — exit 0 unconditionally.
    """
    import json as _json
    from datetime import datetime, timezone

    payload_raw = ""
    if not sys.stdin.isatty():
        try:
            payload_raw = sys.stdin.read()
        except Exception:
            payload_raw = ""

    if not payload_raw.strip():
        return 0

    try:
        data = _json.loads(payload_raw)
    except _json.JSONDecodeError:
        return 0

    event = data.get("hook_event_name") or data.get("event") or "stop"
    session_id = data.get("session_id") or ""

    out = Path.home() / ".teammate-mcp" / "claude-events.jsonl"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a", encoding="utf-8") as f:
            f.write(_json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "event": event,
                "session_id": session_id,
                "raw": data,
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass
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
    if cmd in ("next-reply", "reply-next"):
        sys.exit(_cmd_next_reply(rest))
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
    if cmd == "spawn":
        sys.exit(_cmd_spawn(rest))
    if cmd == "spawned":
        sys.exit(_cmd_spawned(rest))
    if cmd == "despawn":
        sys.exit(_cmd_despawn(rest))
    if cmd in ("close-pane", "close"):
        sys.exit(_cmd_close_pane(rest))
    if cmd in ("codex-notify", "notify-codex"):
        sys.exit(_cmd_codex_notify(rest))
    if cmd in ("claude-notify", "notify-claude"):
        sys.exit(_cmd_claude_notify(rest))
    if cmd == "daemon":
        from .daemon import main as daemon_main
        sys.exit(daemon_main(rest))
    if cmd == "daemon-health":
        from . import daemon_client
        h = daemon_client.health()
        if h is None:
            print("daemon unreachable (not running or TEAMMATE_DAEMON not set)",
                  file=sys.stderr)
            sys.exit(1)
        print(json.dumps(h, indent=2))
        sys.exit(0)

    print(f"unknown subcommand: {cmd!r}", file=sys.stderr)
    print(HELP, file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
