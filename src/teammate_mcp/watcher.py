"""Watchdog daemon — wakes idle Claude Code panes when mail arrives.

Run as: ``teammate-mcp watch [--interval 2.0]``

Behavior loop (every ``interval`` seconds):

  1. Prune dead registry entries.
  2. For each label whose ``job`` is "Python" (i.e. a Claude Code
     pane that almost certainly has the inbox-drain hook installed):
       a. Check ``~/.teammate-mcp/mailbox/<label>/inbox/`` for new
          (since-last-seen) message files.
       b. If new mail and the pane's compose box looks empty, inject
          ``/drain\\r`` to trigger a UserPromptSubmit and let the hook
          drain the inbox.
       c. If the compose box looks busy (user typing), skip — the
          hook will fire whenever the user does submit, and the mail
          will be drained then.
  3. Logs every wake / skip to ``~/.teammate-mcp/logs/watchdog.log``
     and (when ``TEAMMATE_LOG_VERBOSE=1``) to stderr.

Compose-empty heuristic: capture the session's last screen lines via
osascript and search for the pattern ``❯`` followed by only whitespace
to the end of the line. False positives (rare cases the cell buffer
hides typed content) just mean we sometimes skip when we shouldn't —
strictly safer than waking on top of user input.

Codex panes don't have a hook system, so we never wake them.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

MAILBOX = Path.home() / ".teammate-mcp" / "mailbox"
LOG = Path.home() / ".teammate-mcp" / "logs" / "watchdog.log"
# Wake text: a one-word prompt that's safe to inject into an empty
# compose box. We pick "." because:
#   - it triggers UserPromptSubmit (so the hook drains the inbox)
#   - the LLM sees both "." (user prompt) and the prepended inbox
#     contents (hook stdout). Almost every LLM correctly ignores the
#     dot and processes the inbox.
#   - if Claude Code recognises a leading "/" as a slash command and
#     rejects it ("Unknown command: /drain"), no submit happens and
#     no hook fires — which defeats the wake. A bare word avoids that.
WAKE_TEXT = "."
DEFAULT_INTERVAL = 2.0

# Heuristic: a Claude Code compose box looks like
#   ❯ <user text>
# spread across one or more lines bounded by horizontal rule lines.
# Compose is "empty" when the line(s) after ❯ contain only whitespace
# / control characters. iTerm's ``contents`` returns the buffer text
# (ANSI stripped); compose lines occasionally render as null-padded
# rows we treat as empty.
_COMPOSE_LINE = re.compile(r"❯\s*(.*)$")


def _log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n"
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
    if os.environ.get("TEAMMATE_LOG_VERBOSE", "").strip() in ("1", "true", "yes"):
        sys.stderr.write(line)


def _capture(session_id: str) -> str:
    script = (
        'tell application "iTerm"\n'
        '    repeat with w in windows\n'
        '        repeat with t in tabs of w\n'
        '            repeat with s in sessions of t\n'
        f'                if (unique id of s) is "{session_id}" then\n'
        '                    return contents of s\n'
        '                end if\n'
        '            end repeat\n'
        '        end repeat\n'
        '    end repeat\n'
        'end tell'
    )
    try:
        r = subprocess.run(["osascript", "-e", script],
                           check=True, capture_output=True, text=True, timeout=5)
        return r.stdout
    except Exception:
        return ""


def _compose_is_empty(session_id: str) -> bool:
    """True if the last few visible lines look like an empty compose box."""
    screen = _capture(session_id)
    if not screen:
        return False  # can't tell — be safe, skip wake
    lines = screen.splitlines()
    # Inspect last 25 lines for any ❯ prompt; treat empty trailing chars
    # (including null-padding) as "no user text".
    for line in reversed(lines[-25:]):
        m = _COMPOSE_LINE.search(line)
        if not m:
            continue
        rest = m.group(1).strip().strip("\x00 ").strip()
        return rest == ""
    return False


def _wake(session_id: str) -> bool:
    """Inject the wake text + Enter via osascript. Single CR sent
    separately so it lands outside iTerm's bracket-paste envelope and
    submits the slash command."""
    body_script = (
        'tell application "iTerm"\n'
        '    repeat with w in windows\n'
        '        repeat with t in tabs of w\n'
        '            repeat with s in sessions of t\n'
        f'                if (unique id of s) is "{session_id}" then\n'
        f'                    tell s to write text "{WAKE_TEXT}" newline NO\n'
        '                    delay 0.05\n'
        '                    tell s to write text (ASCII character 13) newline NO\n'
        '                end if\n'
        '            end repeat\n'
        '        end repeat\n'
        '    end repeat\n'
        'end tell'
    )
    try:
        subprocess.run(["osascript", "-e", body_script],
                       check=True, capture_output=True, text=True, timeout=10)
        return True
    except Exception as e:
        _log(f"wake-failed sid={session_id[:8]} err={e!r}")
        return False


def _alive_session_ids() -> set[str]:
    script = (
        'tell application "iTerm"\n'
        '    set out to ""\n'
        '    repeat with w in windows\n'
        '        repeat with t in tabs of w\n'
        '            repeat with s in sessions of t\n'
        '                set out to out & (unique id of s) & "\n"\n'
        '            end repeat\n'
        '        end repeat\n'
        '    end repeat\n'
        '    return out\n'
        'end tell'
    )
    try:
        r = subprocess.run(["osascript", "-e", script],
                           check=True, capture_output=True, text=True, timeout=5)
        return {ln.strip().upper() for ln in r.stdout.splitlines() if ln.strip()}
    except Exception:
        return set()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="teammate-mcp watch",
                                     description="Wake idle Claude Code panes when their inbox grows.")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                        help="seconds between mailbox scans (default 2.0)")
    parser.add_argument("--once", action="store_true",
                        help="run a single scan then exit (for tests / smoke)")
    args = parser.parse_args(argv)

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from teammate_mcp import registry  # noqa: E402

    # Identify the pane that LAUNCHED this watchdog so we never inject
    # wake keystrokes back into it — the user is actively typing here.
    # We resolve via TERM_SESSION_ID inherited from the parent shell.
    self_sid = ""
    tsid = os.environ.get("TERM_SESSION_ID", "")
    if tsid:
        self_sid = (tsid.split(":", 1)[1] if ":" in tsid else tsid).upper()

    # last_wake[label] = (mtime, attempted_at). We treat a label as
    # "needing wake" if the inbox has any file with mtime > last_wake.
    # This avoids both (a) re-waking forever on a single stuck file
    # and (b) missing new files because seen-set still remembers old
    # ones. Cooldown also prevents spam during the receiver's
    # multi-second LLM processing window.
    last_wake: dict[str, float] = {}
    COOLDOWN = 6.0  # seconds — must be > 1 LLM round trip
    _log(f"watchdog start interval={args.interval}s self_sid={self_sid[:8] or '(unknown)'}")

    def _scan_once() -> int:
        registry.prune_dead(force_refresh=True)
        labels = registry.all_labels()
        alive = _alive_session_ids()
        woken = 0
        for label, rec in labels.items():
            sid = (rec.get("session_id") or "").upper()
            if sid not in alive:
                continue
            if self_sid and sid == self_sid:
                # The watchdog's launching pane — never wake here. The
                # user is actively typing in it, so the hook will fire
                # naturally on their next prompt.
                continue
            # We don't filter by job anymore — the registry's job field
            # often lags reality (e.g. tmclaude's register-pane runs
            # while the shell is still zsh, then exec claude replaces
            # the shell but the registry keeps "zsh"). Wake all alive
            # panes; if a pane has no hook (codex / plain shell), the
            # injected "." just becomes a harmless prompt that the
            # underlying TUI either echoes or ignores. Compose-empty
            # detection still gates against busy panes.
            inbox = MAILBOX / label / "inbox"
            if not inbox.exists():
                continue
            files = list(inbox.glob("*.json"))
            if not files:
                continue
            newest_mtime = max(f.stat().st_mtime for f in files)
            now = time.time()
            since_last = now - last_wake.get(label, 0.0)
            if newest_mtime <= last_wake.get(label, 0.0):
                # No file newer than the last wake — already in
                # someone's processing pipeline.
                continue
            if since_last < COOLDOWN:
                # Cooldown — give the receiver time to finish its LLM
                # turn before we poke it again.
                continue
            if _compose_is_empty(sid):
                if _wake(sid):
                    _log(f"woke label={label} for {len(files)} pending msg")
                    woken += 1
                    last_wake[label] = now
                else:
                    _log(f"wake-attempt-failed label={label}")
            else:
                _log(f"skip-busy label={label} ({len(files)} pending msg)")
        return woken

    if args.once:
        n = _scan_once()
        _log(f"once-scan done woken={n}")
        return 0

    while True:
        try:
            _scan_once()
        except KeyboardInterrupt:
            _log("watchdog stopped via SIGINT")
            return 0
        except Exception as e:
            _log(f"scan-error {e!r}")
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
