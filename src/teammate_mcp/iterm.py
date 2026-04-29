"""iTerm Python API wrapper.

Identifies sessions by the running process (jobName) so users do not need to
label panes manually. Falls back to jobCommand substring matching if jobName
is hidden behind a wrapper (e.g. tmux, login shell).
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from typing import Iterable, Optional

import iterm2

from . import registry as _registry

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover - psutil is a runtime dep
    psutil = None  # type: ignore


# ===========================================================================
# OSASCRIPT FALLBACK
# ---------------------------------------------------------------------------
# The iterm2 Python lib's `async_get_app(connection)` and per-session
# variable queries can hang on desktops with many panes. AppleScript via
# `osascript` is unaffected because it talks to iTerm directly through
# the Apple Event API, which short-circuits to the requested session by
# id. We use osascript for send_text + capture in the hot path; the
# Python API is still used for spawn-time enumeration where it is fine.
# ===========================================================================

def _applescript_escape(text: str) -> str:
    """Escape a string for use inside an AppleScript double-quoted string."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _raise_iterm_window_for_session(session_id: str) -> bool:
    """Bring the iTerm window containing ``session_id`` to the front.

    Pure AppleScript ``select`` cannot raise a non-frontmost iTerm window;
    only the iterm2 Python API can. We call it in a one-shot blocking
    fashion via ``run_until_complete``. Returns True on success.
    """
    target_sid = session_id.upper()

    async def _go(connection):
        app = await iterm2.async_get_app(connection)
        for w in app.windows:
            for t in w.tabs:
                for s in t.sessions:
                    if s.session_id.upper() == target_sid:
                        await w.async_activate()
                        return True
        return False

    try:
        result = {"ok": False}

        async def runner(connection):
            result["ok"] = await _go(connection)

        iterm2.run_until_complete(runner)
        return result["ok"]
    except Exception:
        return False


def osa_send_text(session_id: str, text: str, submit: bool = True) -> None:
    """Push ``text`` into the iTerm session whose unique id matches
    ``session_id``, optionally followed by a real Enter keystroke.

    ZERO-FOCUS SUBMIT
    -----------------
    iTerm bracket-paste-wraps multi-byte API sends when the target TUI
    has bracket-paste mode enabled (``\\e[?2004h``). Inside that envelope,
    embedded CR/LF are treated as literal characters by raw-mode TUIs
    (codex / Claude Code via crossterm/Ink), so the prompt sits in the
    input box without submitting.

    The trick: send the body in one call, then a *single*-byte ``\\r`` in
    a SEPARATE call. iTerm does not bracket-paste lone single-byte sends,
    so the CR arrives outside the paste envelope and the TUI parser
    treats it as Return → submit. No keyDown, no focus change, no
    AppleScript ``activate``, no Window-Server interaction at all.

    Multi-line / Unicode prompts go through a tempfile so AppleScript
    string escaping is sidestepped.
    """
    import tempfile
    body = text.rstrip("\r\n")
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                      delete=False, suffix=".tmm") as f:
        f.write(body)
        path = f.name

    if submit:
        # Step 1 + 2 in one osascript invocation: write the body, then
        # write a *separate* single-byte CR (ASCII 13). iTerm wraps the
        # body in bracket-paste markers (it's >1 byte), but the lone CR
        # is sent raw, so the TUI sees Enter outside the paste envelope.
        script = f'''
set theText to (read POSIX file "{path}" as «class utf8»)
tell application "iTerm"
    repeat with w in windows
        repeat with t in tabs of w
            repeat with s in sessions of t
                if (unique id of s) is "{session_id}" then
                    tell s to write text theText newline NO
                    delay 0.05
                    tell s to write text (ASCII character 13) newline NO
                end if
            end repeat
        end repeat
    end repeat
end tell
'''
    else:
        script = f'''
set theText to (read POSIX file "{path}" as «class utf8»)
tell application "iTerm"
    repeat with w in windows
        repeat with t in tabs of w
            repeat with s in sessions of t
                if (unique id of s) is "{session_id}" then
                    tell s to write text theText newline NO
                end if
            end repeat
        end repeat
    end repeat
end tell
'''
    try:
        subprocess.run(["osascript", "-e", script], check=True,
                       capture_output=True, text=True, timeout=15)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def osa_capture(session_id: str) -> str:
    """Return the visible buffer of an iTerm session via AppleScript.

    Uses iTerm's ``contents`` property which returns the full screen
    + scrollback as a single string (ANSI stripped by iTerm itself)."""
    script = f'''
tell application "iTerm"
    repeat with w in windows
        repeat with t in tabs of w
            repeat with s in sessions of t
                if (unique id of s) is "{session_id}" then
                    return contents of s
                end if
            end repeat
        end repeat
    end repeat
end tell
'''
    try:
        out = subprocess.run(
            ["osascript", "-e", script],
            check=True, capture_output=True, text=True, timeout=10,
        )
        return out.stdout
    except subprocess.CalledProcessError:
        return ""
    except subprocess.TimeoutExpired:
        return ""


def osa_session_alive(session_id: str) -> bool:
    """Return True iff an iTerm session with the given unique id exists."""
    script = f'''
tell application "iTerm"
    repeat with w in windows
        repeat with t in tabs of w
            repeat with s in sessions of t
                if (unique id of s) is "{session_id}" then
                    return "yes"
                end if
            end repeat
        end repeat
    end repeat
end tell
return "no"
'''
    try:
        out = subprocess.run(
            ["osascript", "-e", script],
            check=True, capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() == "yes"
    except Exception:
        return False


async def osa_wait_for_marker(
    session_id: str, marker: str,
    timeout: float = 300.0, poll_interval: float = 1.5, min_count: int = 1,
) -> Optional[str]:
    """Async wait for ``marker`` in the session screen, ``min_count`` times."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        screen = osa_capture(session_id)
        if screen and screen.count(marker) >= min_count:
            return screen
        await asyncio.sleep(poll_interval)
    return None


# ANSI escape sequence stripper — keeps marker matching robust on
# colored terminal output.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


@dataclass
class SessionRef:
    """Lightweight handle so callers don't need an iterm2 import."""

    session: iterm2.Session
    session_id: str
    name: str
    job: str
    command_line: str
    cwd: Optional[str]


async def _safe_var(session: iterm2.Session, name: str) -> str:
    try:
        v = await session.async_get_variable(name)
        return v or ""
    except Exception:
        return ""


async def _session_by_id(connection, session_id: str) -> Optional[SessionRef]:
    """Cheap lookup: walk the iTerm tree for one matching session_id and
    build only that SessionRef.

    Critical: we deliberately do NOT fetch jobName/commandLine/path here.
    On desktops with many panes those variable queries can hang (the
    iterm2 lib blocks waiting for a slow pane to respond). For the
    routing fast path we only need ``session.async_send_text``, which
    works on the bare Session object — no variables required."""
    sid_up = session_id.upper()
    app = await iterm2.async_get_app(connection)
    for window in app.windows:
        for tab in window.tabs:
            for session in tab.sessions:
                if session.session_id.upper() == sid_up:
                    return SessionRef(
                        session=session,
                        session_id=session.session_id,
                        name=session.name or "",
                        job="",            # not fetched — see docstring
                        command_line="",   # not fetched
                        cwd=None,          # not fetched
                    )
    return None


async def list_sessions(connection) -> list[SessionRef]:
    """Enumerate all live sessions across all windows + tabs."""
    app = await iterm2.async_get_app(connection)
    out: list[SessionRef] = []
    for window in app.windows:
        for tab in window.tabs:
            for session in tab.sessions:
                out.append(
                    SessionRef(
                        session=session,
                        session_id=session.session_id,
                        name=session.name or "",
                        job=await _safe_var(session, "jobName"),
                        command_line=await _safe_var(session, "commandLine"),
                        cwd=(await _safe_var(session, "path")) or None,
                    )
                )
    return out


# ---------------------------------------------------------------------------
# Multi-criteria pane lookup. ``target`` may be a label (registered via the
# TEAMMATE_LABEL env var or the register_self tool), an iTerm session name
# (the one users edit with ``cmd+I``), or any prefix of a session UUID.
# ---------------------------------------------------------------------------

async def find_pane(connection, target: str) -> Optional[SessionRef]:
    """Locate a pane by label OR session name OR session-id prefix.

    Resolution order:
        1. registered label (fast path — looks up session_id directly,
           avoids the expensive enumeration of every iTerm session on
           the user's desktop)
        2. iTerm session name (case-insensitive, exact match)
        3. session_id prefix (≥ 6 chars, case-insensitive)
        4. session name substring (last-resort fuzzy match)
    """
    if not target:
        return None
    needle = target.strip()

    # 1. Fast path: registered label → resolve directly via session_id.
    rec = _registry.lookup(needle)
    if rec:
        sid = (rec.get("session_id") or "").strip()
        if sid:
            ref = await _session_by_id(connection, sid)
            if ref is not None:
                return ref

    # Slow path: enumerate every session to fuzzy-match.
    refs = await list_sessions(connection)
    by_id = {r.session_id.upper(): r for r in refs}

    # 2. session name exact (case-insensitive)
    for r in refs:
        if r.name and r.name.lower() == needle.lower():
            return r

    # 3. session_id prefix / suffix
    needle_up = needle.upper()
    if len(needle_up) >= 6:
        for r in refs:
            sid = r.session_id.upper()
            if sid.startswith(needle_up) or sid.endswith(needle_up) or needle_up in sid:
                return r

    # 4. session name substring (case-insensitive)
    for r in refs:
        if r.name and needle.lower() in r.name.lower():
            return r

    return None


async def describe_panes(connection) -> list[dict]:
    """Return all live panes plus their associated label (if any) and the
    full set of identifiers a caller can use to address them."""
    refs = await list_sessions(connection)
    label_by_sid: dict[str, str] = {}
    for label, rec in _registry.all_labels().items():
        sid = (rec.get("session_id") or "").upper()
        if sid:
            label_by_sid[sid] = label

    out = []
    for r in refs:
        out.append(
            {
                "label": label_by_sid.get(r.session_id.upper()),
                "session_name": r.name or None,
                "session_id": r.session_id,
                "job": r.job,
                "cwd": r.cwd,
            }
        )
    return out


def _command_matches(command_line: str, target: str) -> bool:
    """Whole-word match for ``target`` inside ``command_line``.

    iTerm reports ``commandLine`` as ``'Python" /path/.../claude ...'`` for
    wrapper-launched CLIs, so a substring check works as long as we also
    guard against false positives like ``'python claude_helper.py'`` by
    requiring word boundaries.
    """
    if not command_line or not target:
        return False
    pattern = rf"(?:^|[/\s\"'\-])({re.escape(target)})(?:$|[\s\"'\-])"
    return re.search(pattern, command_line, re.IGNORECASE) is not None


def _candidate_term_session_ids(target_names: Iterable[str]) -> list[str]:
    """Walk the process table; for any process whose ``comm`` looks like
    one of ``target_names``, return the ``TERM_SESSION_ID`` exported by iTerm.

    This finds CLIs that are wrapped (tmux, login shells, screen) where iTerm's
    ``jobName`` only sees the wrapper. iTerm exports ``TERM_SESSION_ID`` to
    every shell it spawns, and that value matches ``session.session_id``.
    """
    if psutil is None:
        return []
    targets = {n.lower() for n in target_names}
    out: list[str] = []
    for proc in psutil.process_iter(attrs=["name", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower()
            cmdline = " ".join(proc.info.get("cmdline") or []).lower()
            if name in targets or any(t in cmdline.split() for t in targets):
                env = proc.environ()
                tsid = env.get("TERM_SESSION_ID")
                if tsid:
                    out.append(tsid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:
            continue
    return out


def _normalize_session_id(raw: str) -> str:
    """iTerm session_id is e.g. ``w0t0p3:95D60D5E-...``; the tail UUID
    portion is what shows up in ``TERM_SESSION_ID``. Compare both halves
    so users on different iTerm versions still match."""
    return raw.strip()


async def find_session_by_job(
    connection,
    job_name: str,
    prefer_cwd: Optional[str] = None,
) -> Optional[SessionRef]:
    """Locate the iTerm pane currently running the given CLI.

    Resolution order (highest priority first):
        1. ``TEAMMATE_<UPPER>_SESSION_ID`` environment override
        2. process-environ lookup → matches by ``TERM_SESSION_ID``
           (works through tmux/screen/login wrappers)
        3. exact jobName + cwd matches prefer_cwd
        4. commandLine word match + cwd matches prefer_cwd
        5. exact jobName match (single)
        6. commandLine word match (single)
        7. exact jobName match (any — pick first)
        8. commandLine word match (any — pick first)
        9. None
    """
    refs = await list_sessions(connection)

    # 1. explicit env override (user can pin a session if auto-detection
    #    misbehaves: TEAMMATE_CLAUDE_SESSION_ID, TEAMMATE_CODEX_SESSION_ID).
    env_key = f"TEAMMATE_{job_name.upper()}_SESSION_ID"
    forced = os.environ.get(env_key)
    if forced:
        forced_norm = _normalize_session_id(forced)
        for r in refs:
            sid = _normalize_session_id(r.session_id)
            if forced_norm == sid or forced_norm in sid or sid.endswith(forced_norm):
                return r

    # 2. process-environ lookup (tmux-wrapper aware). Prefer matches whose
    #    iTerm cwd equals ``prefer_cwd`` so we never grab a *different*
    #    instance of the same CLI running elsewhere on the user's desktop.
    tsids = _candidate_term_session_ids([job_name])
    if tsids:
        norm_tsids = {_normalize_session_id(t) for t in tsids}
        matches = []
        for r in refs:
            sid = _normalize_session_id(r.session_id)
            if any(t == sid or sid.endswith(t) or t.endswith(sid) for t in norm_tsids):
                matches.append(r)
        if matches:
            if prefer_cwd:
                target = os.path.realpath(prefer_cwd)
                cwd_hits = [
                    r for r in matches
                    if r.cwd and os.path.realpath(r.cwd) == target
                ]
                if cwd_hits:
                    return cwd_hits[0]
            return matches[0]

    # 3+ jobName / commandLine fallbacks.
    exact = [r for r in refs if r.job == job_name]
    cmd_hits = [r for r in refs if _command_matches(r.command_line, job_name)]

    def _filter_cwd(rs):
        if not prefer_cwd:
            return []
        target = os.path.realpath(prefer_cwd)
        return [r for r in rs if r.cwd and os.path.realpath(r.cwd) == target]

    cwd_exact = _filter_cwd(exact)
    if cwd_exact:
        return cwd_exact[0]
    cwd_cmd = _filter_cwd(cmd_hits)
    if cwd_cmd:
        return cwd_cmd[0]
    if len(exact) == 1:
        return exact[0]
    if len(cmd_hits) == 1:
        return cmd_hits[0]
    if exact:
        return exact[0]
    if cmd_hits:
        return cmd_hits[0]
    return None


async def send_text(session_ref: SessionRef, text: str, submit: bool = True) -> None:
    """Push text into the target session.

    By default also presses Enter (CR) so TUI prompts (Claude Code, Codex,
    ncurses apps) actually submit the line — a bare ``\\n`` is treated as
    "newline within field" by those UIs, which leaves the prompt sitting
    in the buffer unsubmitted.

    Submit handling: we send the body, briefly yield, then issue a CR.
    Some TUIs (notably Codex) only commit on a *standalone* Enter that is
    distinct from the typing buffer, so we add a tiny gap.
    """
    body = text.rstrip("\r\n")
    await session_ref.session.async_send_text(body)
    if submit:
        # Settle keypress timing — without this, fast-typing CLIs like
        # Codex sometimes treat the trailing CR as part of the same
        # paste and never trigger their submit path.
        await asyncio.sleep(0.25)
        await session_ref.session.async_send_text("\r")


async def clear_input(session_ref: SessionRef) -> None:
    """Best-effort: cancel any half-typed input + IME composition before
    we push our own text. ESC closes IME / popups; Ctrl+U erases the
    current line in most readline-style prompts."""
    # ESC twice (closes IME composition + any popup) then Ctrl+U.
    await session_ref.session.async_send_text("\x1b\x1b")
    await asyncio.sleep(0.1)
    await session_ref.session.async_send_text("\x15")
    await asyncio.sleep(0.1)


async def get_screen(session_ref: SessionRef, n_lines: int = 200) -> str:
    """Read up to N most-recent lines of the target session."""
    contents = await session_ref.session.async_get_screen_contents()
    total = contents.number_of_lines
    start = max(0, total - n_lines)
    rows = []
    for i in range(start, total):
        rows.append(contents.line(i).string)
    return strip_ansi("\n".join(rows))


async def wait_for_marker(
    session_ref: SessionRef,
    marker: str,
    timeout: float = 300.0,
    poll_interval: float = 1.5,
    min_count: int = 1,
) -> Optional[str]:
    """Block until ``marker`` appears at least ``min_count`` times.

    The ``min_count`` knob is critical when the prompt that triggers the
    response itself contains the marker text (it will be echoed in the
    target pane). Set ``min_count=2`` so we only return once a *second*
    occurrence (= the actual reply) shows up.

    Returns the screen text containing the marker (ANSI stripped), or
    ``None`` on timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        screen = await get_screen(session_ref, n_lines=600)
        if screen.count(marker) >= min_count:
            return screen
        await asyncio.sleep(poll_interval)
    return None


def extract_answer(screen: str, question: str, marker: str) -> str:
    """Slice the answer text from a captured screen.

    The screen typically contains the marker *twice*: once because the
    prompt we injected literally contained the marker text (echoed by the
    target TUI), and once because the agent emitted the marker at the end
    of its real reply. The text **between those two occurrences** is the
    answer.

    Fallback: when only a single marker is found, use the locator-based
    slice (everything between the last echo of the question and the
    marker) — this is what the unit tests cover.
    """
    if marker not in screen:
        return ""

    parts = screen.split(marker)
    # `parts[0]` = before first marker (prompt echo header)
    # `parts[1]` = between first and second marker (← the real answer)
    # `parts[2:]` = after the second marker (next prompt etc.)
    if len(parts) >= 3:
        # Strip ANSI box-drawing leftovers and "•"-style bullets that some
        # TUIs prepend to assistant turns.
        return parts[1].strip()

    # Single-marker path (covers our unit tests).
    pre = parts[0]
    locator = question.strip().splitlines()[0][:80] if question.strip() else ""
    if locator and locator in pre:
        answer = pre.split(locator, 1)[-1]
    else:
        answer = pre[-4096:]
    return answer.strip()


# ---------------------------------------------------------------------------
# Convenience helper used by `bin/team` to spawn a window. We deliberately do
# this with AppleScript (subprocess) because the iTerm Python API requires
# the API explicitly enabled, and `team` should work even before that toggle.
# ---------------------------------------------------------------------------

def split_pane_applescript(
    cwd: str,
    left_command: str = "claude",
    right_command: str = "codex",
) -> str:
    """Render the AppleScript snippet that spawns the team layout."""
    return f"""
tell application "iTerm"
    activate
    set newWindow to (create window with default profile)
    tell current session of newWindow
        write text "cd {cwd!s}"
        write text "{left_command}"
    end tell
    tell current tab of newWindow
        set rightSession to (split vertically with default profile)
        tell rightSession
            write text "cd {cwd!s}"
            write text "{right_command}"
        end tell
    end tell
end tell
""".strip()
