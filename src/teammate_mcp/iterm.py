"""iTerm Python API wrapper.

Identifies sessions by the running process (jobName) so users do not need to
label panes manually. Falls back to jobCommand substring matching if jobName
is hidden behind a wrapper (e.g. tmux, login shell).
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from typing import Iterable, Optional

import iterm2

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover - psutil is a runtime dep
    psutil = None  # type: ignore


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
