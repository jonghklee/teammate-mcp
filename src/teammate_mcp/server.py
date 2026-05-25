"""FastMCP server exposing inter-agent Q&A tools.

Exposes:
    ask_codex(question, timeout)     — Claude → Codex
    ask_claude(question, timeout)    — Codex → Claude
    broadcast(message)               — fire-and-forget to both panes
    queue_status()                   — debugging snapshot

The MCP server is a long-lived stdio process spawned by Claude/Codex when
they start. Each tool call opens a short-lived iTerm Python API connection,
locates the target pane by `jobName`, pushes the question with a unique
marker, polls for the marker, then returns the extracted answer.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import iterm2
from mcp.server.fastmcp import FastMCP

from .iterm import (
    SessionRef,
    describe_panes,
    extract_answer,
    find_pane,
    find_session_by_job,
    get_screen,
    osa_capture,
    osa_clear_and_inject,
    osa_extract_compose,
    osa_send_raw,
    osa_send_text,
    osa_session_alive,
    osa_wait_for_marker,
    send_text,
    wait_for_marker,
)
from .log import get_logger
from .queue import MessageQueue
from . import registry


# Configurable through env so users can flip audit mode without code edits.
QUEUE_MODE = os.environ.get("TEAMMATE_QUEUE_MODE", "ephemeral")
PROJECT_CWD = os.environ.get("TEAMMATE_CWD") or os.getcwd()

# Mailbox root — daemonless persistent queue (CCB-style serial-per-agent
# inbox/processed directories).
MAILBOX_ROOT = Path.home() / ".teammate-mcp" / "mailbox"
SPOOL_ROOT = Path.home() / ".teammate-mcp" / "spool"

# Bodies above this size get spilled to a markdown file, and the
# inject becomes a short reference: "본문은 <path> 에 있습니다".
# Default 2 KB — small enough that compose-merge / clear cost is
# trivial, big enough that normal questions still inline.
SPILL_THRESHOLD = int(os.environ.get("TEAMMATE_SPILL_THRESHOLD", "2048"))


def _spill_body(job_id: str, from_agent: str, addressee: str, body: str) -> Path:
    """Write the full body to ~/.teammate-mcp/spool/<job_id>.md.

    Returns the absolute path. Receiver's LLM is instructed to read
    this file via the short reference inject.
    """
    SPOOL_ROOT.mkdir(parents=True, exist_ok=True)
    p = SPOOL_ROOT / f"{job_id}.md"
    header = (
        f"<!-- teammate-mcp spool\n"
        f"     job_id : {job_id}\n"
        f"     from   : {from_agent}\n"
        f"     to     : {addressee}\n"
        f"     bytes  : {len(body.encode('utf-8'))}\n"
        f"-->\n\n"
    )
    p.write_text(header + body, encoding="utf-8")
    return p


# (DANGER_PATTERNS + _wait_until_safe 제거됨 — 2026-05-14, picker detection 과 함께)
# 옛 패턴: receiver pane 의 *위험한* screen 상태 (permission menu, AskUserQuestion,
# y/n 확인, Password, REPL 등) 감지하면 inject 중단. picker detection 과 동일
# 휴리스틱 — *환경 차원에서* picker 안 뜨게 강제했으니 (Claude Code deny +
# codex yolo) 이 방어막도 dead. 호출처도 없는 dead code 였음.


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _mailbox_dir(label: str, sub: str) -> Path:
    p = MAILBOX_ROOT / label / sub
    p.mkdir(parents=True, exist_ok=True)
    return p


@contextlib.contextmanager
def _per_target_send_lock(target_label: str, max_wait: float = 60.0):
    """Cross-process exclusive lock per target pane.

    Two callers (in separate Python processes — e.g. two CLI invocations
    from two different agents) can race on the same receiver: each
    snapshots the compose box, then both clear+inject, and the second
    one's restore overwrites the first one's. asyncio.Lock can't
    serialise that because the processes don't share an event loop.

    flock() does. Each target gets its own lock file at
    ``~/.teammate-mcp/mailbox/<label>/.send-lock``. We block (with a
    cap) until the lock is acquired, perform snapshot→clear→inject→
    sleep→restore inside the lock, then release. Any second sender
    waits in line and operates on whatever the compose box looks like
    AFTER the first one has fully finished its restore — so the
    second sender's snapshot includes whatever the user typed during
    the first send PLUS the first sender's restore — i.e. the live
    state, not stale.
    """
    target_dir = MAILBOX_ROOT / target_label
    target_dir.mkdir(parents=True, exist_ok=True)
    lock_path = target_dir / ".send-lock"
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + max_wait
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EACCES):
                    raise
                if time.monotonic() >= deadline:
                    # Time-out — proceed without the lock rather than
                    # silently drop the message. The caller's snapshot
                    # may collide; surface this in the log.
                    break
                time.sleep(0.1)
        yield acquired
    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except Exception:
                pass
        os.close(fd)


def _write_inbox(target_label: str, record: dict) -> Path:
    """Atomically write a job record into <target>'s inbox/."""
    inbox = _mailbox_dir(target_label, "inbox")
    final = inbox / f"{record['job_id']}.json"
    tmp = inbox / f".{record['job_id']}.json.tmp"
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(final)
    return final


def _move_to_processed(target_label: str, job_id: str, terminal: dict) -> None:
    src = _mailbox_dir(target_label, "inbox") / f"{job_id}.json"
    dst = _mailbox_dir(target_label, "processed") / f"{job_id}.json"
    if src.exists():
        try:
            data = json.loads(src.read_text(encoding="utf-8"))
        except Exception:
            data = {"job_id": job_id}
        data["terminal"] = terminal
        dst.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        src.unlink(missing_ok=True)


def _list_inbox(label: str) -> list[dict]:
    inbox = _mailbox_dir(label, "inbox")
    out = []
    for p in sorted(inbox.glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


# (picker detection 코드 제거됨 — 2026-05-14)
#
# 옛 패턴: screen text 휴리스틱 (_PICKER_PATTERNS / _MENU_LINE_PREFIXES /
# _MENU_WHITELIST / _pane_looks_like_picker) 으로 picker 추측. agent 응답의
# markdown 번호 리스트 (1./2./3.) 가 picker 로 오인되는 false positive +
# 새 phrase 마다 whitelist 무한 patch 가 본질적 한계였음.
#
# 새 전제: caller 가 picker UI 없는 환경을 *환경 차원에서 강제*:
#   - Claude Code: settings.json 에 permissions.deny=["AskUserQuestion"]
#   - codex: --dangerously-bypass-approvals-and-sandbox (yolo flag)
# → keystroke 항상 inject, false positive 영원히 0.
#
# picker UI 가 환경에 다시 도입되어야 한다면 그때 추가.



def _sanitize_for_inject(text: str) -> str:
    """Strip control bytes that would corrupt a receiver TUI's parser.

    Removes:
      - ESC (``\\x1b``) — could start an ANSI escape sequence
      - BEL (``\\x07``) — terminates OSC sequences, can wedge state
      - NUL (``\\x00``) — undefined behavior in most line disciplines
      - other C0 controls except TAB / LF / CR / vertical-tab

    Newlines and tabs are preserved because they are legitimate body
    content. We replace each stripped byte with a single space so
    visible character counts and approximate layout are unchanged.

    This is Fix C in the freeze investigation: if a user-provided ASK
    body contained an unbalanced escape (e.g. ``\\x1b[200~`` starting
    paste-mode), the receiver Claude/Codex TUI could end up wedged in
    a phantom paste state where every key disappears.
    """
    if not text:
        return text
    out = []
    for ch in text:
        code = ord(ch)
        if ch in ("\t", "\n", "\r", "\v"):
            out.append(ch)
        elif code < 0x20 or code == 0x7f:
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def _body_stuck_in_compose(pane_content: str, job_id: str) -> bool:
    """Return True when an injected ASK body is still sitting in compose.

    Only the visible tail is considered for busy markers. Full scrollback
    often contains old Claude status text ("Brewed", "✻", etc.); treating
    that as current processing caused the force-Enter recovery to skip a
    genuinely stuck compose box.
    """
    stuck_marker = f"[teammate-mcp ASK {job_id}"
    if stuck_marker not in pane_content:
        return False

    lines = pane_content.splitlines()
    marker_idx = -1
    for i in range(len(lines) - 1, -1, -1):
        if stuck_marker in lines[i]:
            marker_idx = i
            break
    if marker_idx < 0:
        return False
    tail = "\n".join(lines[marker_idx:][-12:])
    busy_markers = (
        "✻", "⏺", "✶", "Worked for", "Brewed for",
        "Thinking", "Running", "thinking", "running",
    )
    if any(b in tail for b in busy_markers):
        return False
    return stuck_marker in tail


def _register_via_daemon_or_direct(
    *,
    label: str,
    session_id: str,
    pid: int,
    job: str,
    cwd: Optional[str],
    extra: Optional[dict] = None,
) -> None:
    """Register a label, preferring the daemon RPC when available.

    Falls back to the legacy direct-file path if the daemon socket is
    missing or unreachable — keeps the system working when the daemon
    isn't running or has crashed.
    """
    from . import daemon_client

    if daemon_client.is_enabled():
        ok = daemon_client.register(
            label=label, session_id=session_id, pid=pid, job=job,
            cwd=cwd, extra=extra or {},
        )
        if ok is not None:
            return

    registry.register(
        label=label, session_id=session_id, pid=pid, job=job, cwd=cwd,
        extra=extra or {}, dedupe_session_id=True,
    )


def archive_label_mailbox(label: str) -> Optional[Path]:
    """Move ~/.teammate-mcp/mailbox/<label>/ aside.

    Called by register-pane and by prune_dead when a label is recycled
    or removed: prevents the "옛 mailbox에 잔존하는 메시지를 새 페인의
    hook이 drain" 혼동.

    Returns the archived path, or None if there was nothing to archive.
    """
    src = MAILBOX_ROOT / label
    if not src.exists() or not src.is_dir():
        return None
    # Skip pure-empty trees (no point archiving)
    has_any = False
    for sub in ("inbox", "processed"):
        d = src / sub
        if d.exists() and any(d.glob("*.json")):
            has_any = True
            break
    if not has_any:
        # Empty mailbox — just remove instead of archiving for tidiness.
        try:
            import shutil
            shutil.rmtree(src, ignore_errors=True)
        except Exception:
            pass
        return None
    archive = MAILBOX_ROOT / f".archived-{label}-{int(time.time())}"
    try:
        src.rename(archive)
        return archive
    except Exception:
        return None


def _jobname_for(agent: str) -> str:
    """Map agent label → expected jobName as iTerm reports it."""
    return {
        "claude": "claude",
        "codex": "codex",
    }.get(agent.lower(), agent.lower())


mcp = FastMCP("teammate")
_log = get_logger()

# Per-target-pane lock. Only one ask at a time per session — concurrent
# asks to the same pane interleave on screen, breaking marker extraction
# (each ask's marker can land between another ask's question + answer).
# The lock serialises sends; a second ask waits for the first to fully
# complete before it injects text.
_pane_locks: dict[str, asyncio.Lock] = {}


def _pane_lock(session_id: str) -> asyncio.Lock:
    sid_key = session_id.upper()
    lock = _pane_locks.get(sid_key)
    if lock is None:
        lock = asyncio.Lock()
        _pane_locks[sid_key] = lock
    return lock
_queue = MessageQueue(mode=QUEUE_MODE)


async def _resolve_target(connection, spec: str, fallback_agent: Optional[str]) -> Optional[SessionRef]:
    """Resolve a target spec to an iTerm session — REGISTRY ONLY.

    Behavior change in v0.3.0: we no longer fall back to scanning the
    process table for any matching jobName. Only panes the user has
    explicitly registered (via ``/team-register`` or ``register_self``)
    are addressable. This matches the user's mental model: "only tagged
    panes participate".

    For ``spec``: try ``find_pane`` (label / session name / id prefix).
    For ``fallback_agent``: look up the registry for any registered pane
    whose recorded ``job`` matches the agent name. Returns None if no
    registered pane matches.
    """
    if spec:
        return await find_pane(connection, spec)

    if fallback_agent:
        wanted = fallback_agent.lower()
        from .iterm import list_sessions
        live_sids = {r.session_id.upper() for r in await list_sessions(connection)}
        for label, rec in registry.all_labels().items():
            if (rec.get("job") or "").lower() != wanted:
                continue
            sid = (rec.get("session_id") or "").upper()
            if sid in live_sids:
                return await find_pane(connection, label)
    return None


def _resolve_target_session_id(target: str, fallback_agent: Optional[str]) -> Optional[str]:
    """Pure-registry, pure-Python target → session_id resolver.

    No iterm2 lib, no AppleScript. Looks up the label in the registry
    and returns its recorded session_id. For the 1:1 fallback, scans
    the registry for any entry whose recorded ``job`` matches the
    requested agent name.

    Used by the new osascript-only ask path; fast and immune to the
    iterm2 lib's per-pane variable-query stalls.
    """
    if target:
        rec = registry.lookup(target)
        if rec:
            sid = (rec.get("session_id") or "").strip()
            if sid:
                return sid
        return None
    if fallback_agent:
        wanted = fallback_agent.lower()
        for rec in registry.all_labels().values():
            if (rec.get("job") or "").lower() == wanted:
                sid = (rec.get("session_id") or "").strip()
                if sid:
                    return sid
    return None


def _ensure_watchdog_running_best_effort() -> None:
    try:
        from .watcher import ensure_watchdog_running
        ensure_watchdog_running()
    except Exception:
        pass


async def _ask_async(
    question: str,
    target: str = "",
    fallback_agent: Optional[str] = None,
    timeout: int = 300,
    safe_max_wait: float = 30.0,
    wait: bool = False,
    mailbox_only: Optional[bool] = None,
) -> str:
    """Drive one ask: enqueue → push (osascript) → return immediately.

    Always async. The message is persisted to
    ``~/.teammate-mcp/mailbox/<target>/inbox/<job_id>.json``; by default
    we ALSO keystroke-inject it into the receiver's compose for immediate
    delivery (``mailbox_only=False``). The receiver replies via its own
    reverse async ask.

    ``mailbox_only`` resolution when not passed explicitly (``None``):
    keystroke-inject by default; set ``TEAMMATE_MCP_MAILBOX_ONLY=1`` to
    fall back to pure mailbox + hook/watcher delivery globally.

    The ``wait`` kwarg is accepted for backwards compatibility with
    older callers but is ignored: every ask is async.
    """
    _ = wait  # accepted-but-ignored, see docstring
    if mailbox_only is None:
        mailbox_only = (
            os.environ.get("TEAMMATE_MCP_MAILBOX_ONLY", "").strip().lower()
            in ("1", "true", "yes", "on")
        )
    addressee = target or fallback_agent or "<unspecified>"
    # Resolve `from_agent` in priority order:
    #   1. explicit env var (TEAMMATE_LABEL) — wrappers may set this
    #   2. registry lookup by caller's TERM_SESSION_ID — most reliable
    #      since the MCP subprocess (and any CLI invocation via the
    #      bash tool) inherits TERM_SESSION_ID from the iTerm shell
    #   3. fallback_agent — set when caller used ask_codex/ask_claude
    #   4. "unknown" — last resort
    from_agent = os.environ.get("TEAMMATE_LABEL", "").strip()
    if not from_agent:
        tsid = os.environ.get("TERM_SESSION_ID", "")
        sid_tail = (tsid.split(":", 1)[1] if ":" in tsid else tsid).upper()
        if sid_tail:
            for label, rec in registry.all_labels().items():
                rec_sid = (rec.get("session_id") or "").upper()
                if rec_sid == sid_tail or rec_sid.endswith(sid_tail) or sid_tail.endswith(rec_sid):
                    from_agent = label
                    break
    if not from_agent:
        from_agent = fallback_agent or "unknown"
    msg = _queue.enqueue(from_agent, addressee, question, timeout=timeout)
    _log.event(
        "ask.enqueue",
        id=msg.id, from_=from_agent, to=addressee,
        target_spec=target or None, len=len(question),
    )

    sid = _resolve_target_session_id(target, fallback_agent)
    _log.event("ask.resolve", id=msg.id, found=sid is not None, session_id=sid)
    if sid is None:
        _queue.fail(msg.id, "session not found")
        _log.event("ask.fail", id=msg.id, reason="not_in_registry", target=addressee)
        if target:
            return (
                f"ERROR: no registered pane matches target {target!r}.\n"
                f"Hint: in the target pane, run `teammate-mcp register-pane` "
                f"(or use the tmclaude/tmcodex wrappers)."
            )
        return (
            f"ERROR: no registered '{fallback_agent}' pane.\n"
            f"Hint: in the {fallback_agent} pane, run `teammate-mcp register-pane`."
        )

    if not osa_session_alive(sid):
        registry.unregister(addressee if target else "")
        _queue.fail(msg.id, "session vanished")
        _log.event("ask.fail", id=msg.id, reason="session_dead", session_id=sid)
        return (
            f"ERROR: registered pane {addressee} (session {sid[:8]}…) is no "
            f"longer open in iTerm. Re-register the new pane."
        )

    # Persist to <target>'s inbox BEFORE attempting injection, so the
    # message is never lost — even if injection is refused due to a
    # danger prompt, the target can pick it up via /inbox or a hook.
    inbox_record = {
        "job_id": msg.id,
        "from_": from_agent,
        "to": addressee,
        "body": question,
        "created_at": _now_iso(),
        "status": "queued",
    }
    try:
        _write_inbox(addressee, inbox_record)
    except Exception as e:
        _log.event("ask.inbox_write_failed", id=msg.id, error=repr(e))

    if mailbox_only:
        _log.event("ask.mailbox_only", id=msg.id, to=addressee)
        _ensure_watchdog_running_best_effort()
        _queue.complete(msg.id, "")
        return f"queued mailbox-only message for {addressee}"

    marker = f"tmdone-{msg.id}-end"
    # Spill huge bodies to disk and inject only a short reference.
    # Threshold gates by *byte* count, not chars, since multibyte
    # Korean inflates fast.
    use_spool = len(question.encode("utf-8")) > SPILL_THRESHOLD
    if use_spool:
        spool_path = _spill_body(msg.id, from_agent, addressee, question)
        body_kernel = (
            f"본문이 길어서 파일로 저장됐어. 이 파일을 읽어 처리해줘:\n"
            f"  {spool_path}\n\n"
            f"(파일 내용 = 본인의 user prompt 라고 생각하면 됨. "
            f"처리 후 파일 그대로 두거나 unlink 가능.)"
        )
        _log.event("ask.spilled", id=msg.id, path=str(spool_path),
                   bytes=len(question.encode("utf-8")))
    else:
        body_kernel = question

    # Fix C: sanitize the body so a stray ESC/BEL in the user's question
    # can't wedge the receiver TUI in a corrupt parser state (e.g. phantom
    # paste-mode where every keystroke disappears).
    body_kernel = _sanitize_for_inject(body_kernel)

    body = (
        f"[teammate-mcp ASK {msg.id} from={from_agent}]\n"
        f"{body_kernel}\n\n"
        f"Reply when you can by calling: "
        f"`mcp__teammate__ask(target='{from_agent}', question='<your reply>')`\n"
        f"Do not use Bash or write XML/tool tags for teammate replies.\n"
        f"(no marker required; the sender is not blocked).\n"
    )

    _queue.claim(msg.id)
    _log.event("ask.send_start", id=msg.id, to=addressee, session_id=sid, wait=wait)

    # ── v0.10.0 LEGACY-FIRST DELIVERY ──────────────────────────────
    # Restore the v0.6 directly-injected keystroke path, but bracket
    # it with compose save & restore so user-typed text isn't lost:
    #   1. snapshot the receiver's compose box
    #   2. ESC ESC + Ctrl+U to clear (Claude Code's own clear sequence)
    #   3. inject body via osascript + lone CR (the v0.6 path)
    #   4. background task: 2 s later, type the saved text back into compose
    #   5. delete the inbox file iff keystroke succeeded so the receiver's
    #      hook (if any) doesn't double-deliver. If keystroke failed,
    #      the inbox file remains and the hook acts as fallback.
    saved_compose = ""
    delivered_via_keystroke = False
    # Acquire per-target cross-process lock. Two senders to the same
    # pane run snapshot→clear→inject→restore strictly in series, so
    # neither one's restore wipes the other one's body.
    def _acquire_and_run():
        """Sync helper because flock + osascript are blocking I/O.
        Returns (saved, delivered)."""
        with _per_target_send_lock(addressee) as got_lock:
            if not got_lock:
                _log.event("ask.lock_timeout_proceeding", id=msg.id,
                           target=addressee)

            # (Fix D — picker detection — removed)
            # 사용자 결정 (2026-05-14): picker UI 자체를 환경에서 강제 차단
            # (Claude Code 의 settings.json deny + codex 의 yolo flag) 하고
            # screen-text 휴리스틱은 완전 제거. picker 없는 환경 전제 →
            # keystroke 무조건 inject, false positive 영원히 0.
            # _PICKER_PATTERNS / _MENU_LINE_PREFIXES / _MENU_WHITELIST /
            # _pane_looks_like_picker 모두 삭제됨.

            # Fix B: mid-typing detection. Two snapshots 300 ms apart;
            # if the compose buffer changed in between, the user is
            # actively typing into the target pane. Injecting then would
            # race their keystrokes and could leave the TUI in a corrupt
            # half-paste state ("this one pane freezes forever").
            #
            # Rather than give up immediately, WAIT for the compose to
            # stabilise (user pauses) — typing pauses are a few seconds,
            # so this just defers the inject briefly. A generous cap
            # (TEAMMATE_INJECT_STABILISE_WAIT, default 20s) protects the
            # rare "walked away with text in the box" case: if it never
            # settles we fall back to the mailbox (the hook delivers it
            # on the user's next prompt). Note: the per-target lock is
            # held during the wait, so concurrent senders to the SAME
            # pane queue behind it — fine for short pauses.
            stabilise_wait = float(
                os.environ.get("TEAMMATE_INJECT_STABILISE_WAIT", "20")
            )
            local_saved = osa_extract_compose(sid)
            deadline = time.monotonic() + stabilise_wait
            waited_for_typing = False
            while True:
                time.sleep(0.30)
                second_snap = osa_extract_compose(sid)
                if local_saved == second_snap:
                    break  # stable — empty box, or user paused typing
                waited_for_typing = True
                local_saved = second_snap
                if time.monotonic() >= deadline:
                    _log.event(
                        "ask.user_typing_wait_timeout",
                        id=msg.id,
                        waited_s=round(stabilise_wait, 1),
                        len=len(second_snap),
                    )
                    return second_snap, False  # fall back to mailbox/hook
            if waited_for_typing:
                _log.event("ask.compose_stabilised", id=msg.id,
                           len=len(local_saved))
            # local_saved now holds the settled compose text.

            clear_count = len(local_saved) + 4 if local_saved else 0
            if local_saved:
                _log.event("ask.compose_snapshot", id=msg.id,
                           saved_len=len(local_saved),
                           preview=local_saved[:40])

            # Fix A: defensive paste-mode terminator. If the pane is
            # somehow already stuck inside paste-mode (e.g. a previous
            # inject lost its end-of-paste marker), this empty
            # ``\x1b[201~`` flushes it out before we type the real body.
            # Harmless when not in paste-mode — every modern TUI ignores
            # an orphan paste-end. Safer than risking a doubly-wedged
            # parser.
            try:
                osa_send_raw(sid, "\x1b[201~")
                time.sleep(0.05)
            except Exception:
                pass  # best-effort; not having this fallback isn't fatal

            try:
                # Single osascript: DEL × clear_count + body + Enter.
                # Saves ~400-600 ms vs. doing them as separate calls.
                osa_clear_and_inject(sid, clear_count, body)
                _log.event("ask.send", id=msg.id, to=addressee,
                           session_id=sid, mode="legacy-keystroke",
                           restoring=bool(local_saved))
            except Exception as e:
                _log.event("ask.send_failed_falling_back_to_file",
                           id=msg.id, error=repr(e))
                return local_saved, False

            # The inject's Enter fires the receiver's UserPromptSubmit hook
            # almost immediately; if the inbox file is still present the
            # hook re-delivers the SAME message as attached context
            # (double-delivery — pervasive now that inject is the default
            # path). So unlink eagerly here. We RE-CREATE the file below
            # only if post-inject verification proves the body never
            # actually submitted — keeping the durable copy for recovery
            # without the silent loss the old unconditional eager-unlink
            # caused.
            try:
                (MAILBOX_ROOT / addressee / "inbox" / f"{msg.id}.json").unlink(
                    missing_ok=True,
                )
                _log.event("ask.inbox_unlinked_inline", id=msg.id)
            except Exception as e:
                _log.event("ask.inbox_unlink_failed", id=msg.id, error=repr(e))

            # Fix E: post-inject verify + force Enter. If the inject didn't
            # submit (long/multiline body, or the TUI treated the trailing
            # newline as paste-end), the body is still visible and the
            # receiver is NOT processing it. Detect, retry Enter, and if
            # it's STILL stuck, recreate the inbox file so a hook/watcher
            # can recover it (loss is worse than a rare double-delivery).
            delivered_ok = True
            try:
                time.sleep(0.5)
                pane_after = osa_capture(sid)
                if _body_stuck_in_compose(pane_after, msg.id):
                    _log.event(
                        "ask.body_stuck_in_compose_forcing_enter",
                        id=msg.id,
                    )
                    recovered = False
                    for attempt in range(3):
                        osa_send_raw(sid, "\r")
                        time.sleep(0.4)
                        check = osa_capture(sid)
                        if not _body_stuck_in_compose(check, msg.id):
                            _log.event(
                                "ask.force_enter_recovered",
                                id=msg.id,
                                attempt=attempt + 1,
                            )
                            recovered = True
                            break
                    delivered_ok = recovered
                    if not recovered:
                        # Never submitted → restore the durable copy we
                        # eagerly removed so it's recoverable.
                        try:
                            _write_inbox(addressee, inbox_record)
                            _log.event(
                                "ask.inbox_recreated_after_stuck", id=msg.id,
                                warning="body stuck after 3 Enter retries; "
                                        "inbox file restored for recovery",
                            )
                        except Exception as e:
                            _log.event("ask.inbox_recreate_failed",
                                       id=msg.id, error=repr(e))
            except Exception as e:
                # Couldn't verify — leave it unlinked (assume delivered)
                # rather than risk duplicate delivery on the common path.
                _log.event("ask.verify_skipped", id=msg.id, error=repr(e))
            if local_saved:
                # 0.15s — Claude Code commits Enter ~100-150ms.
                time.sleep(0.15)
                try:
                    if "\n" in local_saved:
                        # Multi-line: each \n must become Shift+Enter
                        # (\x1b\r) — a bare \n would submit the body
                        # mid-restore. send_raw bypasses the
                        # bracket-paste wrap that splits a paste's
                        # final \r off as a real Enter.
                        seq = local_saved.replace("\n", "\x1b\r")
                        osa_send_raw(sid, seq)
                    else:
                        osa_send_text(sid, local_saved, False)
                    _log.event("ask.compose_restored", id=msg.id,
                               restored_len=len(local_saved),
                               multiline="\n" in local_saved)
                except Exception as e:
                    _log.event("ask.restore_failed",
                               id=msg.id, error=repr(e))
                # No settling sleep — guard catches stale-echo
                # snapshots, and the next sender's snapshot can
                # tolerate transient repaint.
            return local_saved, delivered_ok

    # Default False so an exception inside the thread leaves us on the
    # safe "file-fallback" path (inbox file kept for hook recovery)
    # instead of raising NameError below.
    saved_compose, delivered_via_keystroke = "", False
    try:
        saved_compose, delivered_via_keystroke = await asyncio.to_thread(
            _acquire_and_run,
        )
    except Exception as e:
        _log.event("ask.send_failed_falling_back_to_file",
                   id=msg.id, error=repr(e))

    if delivered_via_keystroke:
        try:
            (MAILBOX_ROOT / addressee / "inbox" / f"{msg.id}.json").unlink(
                missing_ok=True,
            )
        except Exception:
            pass

    # Always async path: kick watchdog so the receiver's hook fires soon,
    # then return immediately. Receiver replies via reverse async ask.
    _ensure_watchdog_running_best_effort()
    _queue.complete(msg.id, "")
    return (f"sent: job_id={msg.id} to {addressee} "
            f"({'keystroke' if delivered_via_keystroke else 'file-fallback'})")


# ---------------------------------------------------------------------------
# MCP tool surface
# ---------------------------------------------------------------------------

@mcp.tool()
async def ask(question: str, target: str = "", timeout: int = 300, wait: bool = False) -> str:
    """Ask another pane a question (always async).

    ``target`` may be:
      * a registered label (set via ``TEAMMATE_LABEL`` env or
        ``register_self``),
      * an iTerm session name (the title users edit with ``cmd+I``,
        case-insensitive exact match),
      * a session UUID prefix (≥ 6 chars).

    The message is persisted to ``~/.teammate-mcp/mailbox/<target>/inbox/``
    AND, by default, keystroke-injected into the receiver's compose for
    immediate delivery. If the inject can't submit (or is disabled via
    ``TEAMMATE_MCP_MAILBOX_ONLY=1``), the durable mailbox copy is drained
    by the receiver's ``UserPromptSubmit`` hook / watchdog / Stop hook
    instead. The receiver replies via a reverse async ``ask``.
    Caller is never blocked, receiver's compose box is never corrupted.

    The ``wait`` parameter is accepted for backwards compatibility but
    has no effect — sync mode was removed because the receiver always
    took the same mailbox path anyway; sync only blocked the sender
    for no real-time benefit.

    When ``target`` is empty the caller's job name is used: a Claude
    caller falls back to "codex" and vice versa, preserving the v0.1
    1:1 default behaviour.
    """
    _ = wait  # deprecated, ignored
    fallback = "codex" if (os.environ.get("TEAMMATE_LABEL") or "").lower().startswith("claude") else None
    if not target and fallback is None:
        fallback = None
    return await _ask_async(question, target=target,
                            fallback_agent=fallback, timeout=timeout)


@mcp.tool()
async def inbox(label: str = "") -> list[dict]:
    """List queued (unprocessed) messages in a pane's inbox.

    If ``label`` is empty, uses the caller's own label (resolved via
    TEAMMATE_LABEL env or by matching TERM_SESSION_ID against the
    registry).

    Each entry has the shape::

        {
          "job_id":    "1777…",
          "from_":     "claude4",
          "to":        "claude20",
          "body":      "<question text>",
          "created_at": "2026-04-29T05:30:12Z",
          "status":    "queued"
        }

    Use this from a receiver pane to drain pending mail when you are
    idle — process each entry and reply via ``ask(target=<from_>,
    question=<reply>)``.
    """
    label = label.strip()
    if not label:
        # Resolve caller label
        label = os.environ.get("TEAMMATE_LABEL", "").strip()
        if not label:
            tsid = os.environ.get("TERM_SESSION_ID", "")
            sid_tail = (tsid.split(":", 1)[1] if ":" in tsid else tsid).upper()
            for lbl, rec in registry.all_labels().items():
                rec_sid = (rec.get("session_id") or "").upper()
                if rec_sid == sid_tail or (sid_tail and rec_sid.endswith(sid_tail)):
                    label = lbl
                    break
        if not label:
            return [{"error": "no caller label resolvable"}]
    return _list_inbox(label)


@mcp.tool()
async def spawn(label: str, command: str, cwd: str = "",
                message: str = "", mode: str = "auto",
                wait_s: int = 20, yolo: bool = False,
                screen: str = "", bounds: str = "") -> str:
    """Spawn a new iTerm pane with a label, then optionally send an
    initial message to it.

    Args:
        label:    Unique label for the new pane (e.g. "worker1").
        command:  Shell command to run (e.g. "claude" or "codex").
        cwd:      Working directory. Tilde-expanded. Defaults to caller's cwd.
        message:  Optional first ask to send after registration.
        mode:     "auto" (default) | "split-v" | "split-h" | "tab" | "window".
                  "auto" uses SORANO-style stacking placement: the FIRST
                  child splits the CALLER's pane (split-v, new right
                  column), each subsequent child splits-h UNDER the last
                  live child this caller spawned. Resolved via
                  TEAMMATE_LABEL/registry — never iTerm's focused pane;
                  falls back to "window" if the caller pane is unknown.
                  Pass an explicit mode to override. Use "window" for a
                  free-standing window (e.g. with screen/bounds).
        wait_s:   Max seconds to wait for registration. Default 20.
        yolo:     If True and command starts with ``codex``, automatically
                  append ``--yolo`` (matches ``tmcodex`` alias). Default False.
        screen:   Named region for a new window — "left" | "right" | "top"
                  | "bottom" | "full". Only valid when mode="window".
                  Overrides ``bounds``.
        bounds:   Pixel rect as ``"x1,y1,x2,y2"`` for the new window. Only
                  valid when mode="window". Ignored if ``screen`` is set.

    Returns a status line describing the spawned pane.
    """
    import asyncio as _asyncio
    args = ["teammate-mcp", "spawn", label, command]
    if cwd:
        args += ["--cwd", cwd]
    # "auto" → omit --mode so the CLI runs its default SORANO stacking
    # placement (mode_explicit stays False). Any explicit mode is
    # forwarded and honored verbatim (including "window").
    if mode and mode != "auto":
        args += ["--mode", mode]
    if wait_s and wait_s != 20:
        args += ["--wait-s", str(int(wait_s))]
    if yolo:
        args += ["--yolo"]
    if screen:
        args += ["--screen", screen]
    elif bounds:
        args += ["--bounds", bounds]
    if message:
        args += ["-m", message]

    proc = await _asyncio.create_subprocess_exec(
        *args,
        stdout=_asyncio.subprocess.PIPE,
        stderr=_asyncio.subprocess.PIPE,
    )
    try:
        out, err = await _asyncio.wait_for(proc.communicate(), timeout=120.0)
    except _asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        return f"ERROR: spawn timed out after 120s"

    stdout = out.decode("utf-8", errors="replace").strip()
    stderr = err.decode("utf-8", errors="replace").strip()
    if proc.returncode == 0:
        return stdout or "ok"
    head = stderr.splitlines()[0] if stderr else stdout
    return f"ERROR (rc={proc.returncode}): {head}"


@mcp.tool()
async def spawned() -> str:
    """List panes spawned by ``mcp__teammate__spawn`` / ``teammate-mcp spawn``.

    Returns JSON output. Each entry includes the label, session_id, cwd,
    original command, mode, spawn time, and a ``status`` field:
      - ``alive`` — pane still open and registered under the same label
      - ``label-reused`` — label exists but on a different session_id
      - ``gone`` — pane closed; entry remains in the ledger until
        purged via ``despawn --gone``
    """
    import asyncio as _asyncio
    proc = await _asyncio.create_subprocess_exec(
        "teammate-mcp", "spawned", "--json",
        stdout=_asyncio.subprocess.PIPE,
        stderr=_asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    return out.decode("utf-8", errors="replace").strip() or "[]"


@mcp.tool()
async def despawn(label: str = "", all_: bool = False, gone: bool = False) -> str:
    """Close (and unregister) panes spawned by this tool.

    Args:
        label:   single label to despawn. Ignored when ``all_`` or
                 ``gone`` is set.
        all_:    if True, despawn every pane recorded in the spawn ledger.
        gone:    if True, purge ledger entries whose pane is already gone
                 (no pane is closed; just cleans up the ledger).

    Returns the despawn command's stdout (summary lines describing what
    was closed). Refuses to close panes the user opened by hand — they
    won't be in the ledger.
    """
    import asyncio as _asyncio
    args = ["teammate-mcp", "despawn"]
    if all_:
        args.append("--all")
    elif gone:
        args.append("--gone")
    elif label:
        args.append(label)
    else:
        return "ERROR: provide label, all_=True, or gone=True"

    proc = await _asyncio.create_subprocess_exec(
        *args,
        stdout=_asyncio.subprocess.PIPE,
        stderr=_asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    stdout = out.decode("utf-8", errors="replace").strip()
    stderr = err.decode("utf-8", errors="replace").strip()
    if proc.returncode == 0:
        return stdout or stderr or "ok"
    return f"ERROR (rc={proc.returncode}): {(stderr or stdout).splitlines()[0] if (stderr or stdout) else 'unknown'}"


@mcp.tool()
async def close_pane(target: str, keep_label: bool = False) -> str:
    """Close an iTerm pane by label or session_id.

    Unlike ``despawn``, this does NOT require the pane to be in the
    spawn ledger — works on any pane you can identify, including ones
    you opened yourself or spawned via a custom skill script.

    Args:
        target:     A registered label, an 8+ char session_id prefix,
                    or a full session_id UUID.
        keep_label: If True, only close the pane; leave its registry
                    entry alone. Default False (drop the label too).

    Returns a short status line. Use this from skill cleanup scripts
    (e.g. ``.claude/skills/<skill>/cleanup_pane.sh``) when you want a
    spawned worker to remove itself after a task completes.
    """
    import asyncio as _asyncio
    args = ["teammate-mcp", "close-pane", target]
    if keep_label:
        args.append("--keep-label")
    proc = await _asyncio.create_subprocess_exec(
        *args,
        stdout=_asyncio.subprocess.PIPE,
        stderr=_asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    stdout = out.decode("utf-8", errors="replace").strip()
    stderr = err.decode("utf-8", errors="replace").strip()
    if proc.returncode == 0:
        return stdout or "ok"
    return f"ERROR (rc={proc.returncode}): {(stderr or stdout).splitlines()[0] if (stderr or stdout) else 'unknown'}"


@mcp.tool()
async def mark_processed(job_id: str, target: str = "", reply: str = "") -> str:
    """Move a job from inbox/ to processed/ on the target's mailbox.

    Call this from the receiver after you've replied (or otherwise
    handled) the message. ``target`` defaults to the caller's own
    label. ``reply`` is stored in the processed record so callers
    waiting via ``watch`` can read it.
    """
    if not target:
        target = os.environ.get("TEAMMATE_LABEL", "").strip()
    if not target:
        return "ERROR: no target label"
    try:
        _move_to_processed(target, job_id,
                           {"status": "completed", "reply": reply,
                            "finished_at": _now_iso()})
        return f"ok: {job_id} moved to processed"
    except Exception as e:
        return f"ERROR: {e!r}"


@mcp.tool()
async def ask_codex(question: str, timeout: int = 300) -> str:
    """Legacy 1:1 helper. Prefer ``ask`` with an explicit ``target``."""
    return await _ask_async(question, timeout, target="", fallback_agent="codex")


@mcp.tool()
async def ask_claude(question: str, timeout: int = 300) -> str:
    """Legacy 1:1 helper. Prefer ``ask`` with an explicit ``target``."""
    return await _ask_async(question, timeout, target="", fallback_agent="claude")


@mcp.tool()
async def list_panes() -> list[dict]:
    """Return every live iTerm pane plus its label/name/id/job/cwd.

    Use this to see what targets are currently addressable. The shape of
    each entry is::

        {
          "label":        "worker"   | None,
          "session_name": "Worker A" | None,
          "session_id":   "B913A27E-…",
          "job":          "codex",
          "cwd":          "/path/…",
        }
    """
    connection = await iterm2.Connection.async_create()
    try:
        return await describe_panes(connection)
    finally:
        try:
            connection.close()
        except Exception:
            pass


@mcp.tool()
async def register_self(label: str = "") -> str:
    """Register the *calling* pane.

    If ``label`` is empty (the default), an auto label is assigned:
    ``claude1`` / ``codex1`` / ``codex2`` / etc., based on the pane's
    job and the next free slot. Subsequent ``ask(target=label, …)``
    calls route to this pane.

    The returned string includes the chosen label so the caller can
    print it back to the user — they should *also* set their iTerm
    tab title to that label so it's visible at the bottom of the pane.
    """
    tsid = os.environ.get("TERM_SESSION_ID", "")
    sid_tail = tsid.split(":", 1)[1] if ":" in tsid else tsid
    if not sid_tail:
        return "ERROR: no TERM_SESSION_ID — are you running inside iTerm?"

    connection = await iterm2.Connection.async_create()
    try:
        from .iterm import list_sessions
        refs = await list_sessions(connection)
        match = None
        for r in refs:
            if r.session_id.upper().endswith(sid_tail.upper()) or r.session_id.upper() == sid_tail.upper():
                match = r
                break
        if match is None:
            return f"ERROR: could not find iTerm session {sid_tail}"

        # Reuse existing label if this pane is already registered.
        existing_label = next(
            (l for l, r in registry.all_labels().items()
             if (r.get("session_id") or "").upper() == match.session_id.upper()),
            None,
        )
        chosen = label.strip() or existing_label or _next_auto_label(match.job, match.name or "")

        _register_via_daemon_or_direct(
            label=chosen,
            session_id=match.session_id,
            pid=os.getpid(),
            job=match.job,
            cwd=match.cwd,
            extra={"session_name": match.name or None,
                   "auto_assigned": not label.strip()},
        )

        # Try to set the iTerm tab title so the label is visible to the
        # user without requiring `bin/install-statusline`.
        try:
            await match.session.async_send_text(
                f"\x1b]2;[{chosen}]\x07"
            )
        except Exception:
            pass

        _log.event("register_self", label=chosen, session_id=match.session_id,
                   auto=not label.strip())
        return f"registered as '{chosen}' (session {match.session_id[:8]}…)"
    finally:
        try:
            connection.close()
        except Exception:
            pass


@mcp.tool()
async def unregister(label: str) -> str:
    """Remove a label from the registry."""
    registry.unregister(label)
    _log.event("unregister", label=label)
    return f"unregistered {label!r}"


@mcp.tool()
async def broadcast(message: str, targets: Optional[list[str]] = None) -> str:
    """Push a message to one or more panes without waiting for a reply.

    If ``targets`` is omitted, broadcasts to claude+codex (legacy mode).
    """
    connection = await iterm2.Connection.async_create()
    try:
        sent: list[str] = []
        if targets:
            for t in targets:
                ref = await find_pane(connection, t)
                if ref is not None:
                    await send_text(ref, f"[teammate-mcp BROADCAST] {message}")
                    sent.append(t)
        else:
            for agent in ("claude", "codex"):
                ref = await find_session_by_job(
                    connection, _jobname_for(agent), prefer_cwd=PROJECT_CWD
                )
                if ref is not None:
                    await send_text(ref, f"[teammate-mcp BROADCAST] {message}")
                    sent.append(agent)
        _log.event("broadcast", to=sent, len=len(message))
        return f"sent to: {', '.join(sent) if sent else 'nobody'}"
    finally:
        try:
            connection.close()
        except Exception:
            pass


@mcp.tool()
def queue_status() -> dict:
    """Return queue counts + recent completions (debugging)."""
    return _queue.status()


async def auto_register_session(connection, session_id: str,
                                 explicit_label: Optional[str] = None) -> Optional[dict]:
    """Register an iTerm pane (by id) into the global registry.

    Used by spawn helpers (`bin/team`, demo scripts) so a pane is
    addressable *immediately* after launch — before its CLI gets a chance
    to invoke any MCP tool. Returns the registered record or None on miss.

    If the pane is already registered (same session_id), reuses the
    existing label instead of inventing a new one — prevents the
    ``agent1`` and ``agent2`` both pointing at the same pane.
    """
    from .iterm import list_sessions
    refs = await list_sessions(connection)
    sid_up = session_id.upper()
    me = next((r for r in refs if r.session_id.upper() == sid_up), None)
    if me is None:
        return None

    # Reuse existing label if this pane is already registered.
    existing_label: Optional[str] = None
    for label, rec in registry.all_labels().items():
        if (rec.get("session_id") or "").upper() == sid_up:
            existing_label = label
            break

    label = explicit_label or existing_label or _next_auto_label(me.job, me.name)
    _register_via_daemon_or_direct(
        label=label,
        session_id=me.session_id,
        pid=os.getpid(),
        job=me.job,
        cwd=me.cwd,
        extra={"session_name": me.name or None,
               "auto_assigned": not explicit_label},
    )
    return {
        "label": label,
        "session_id": me.session_id,
        "job": me.job,
        "cwd": me.cwd,
    }


def _classify(job: str, session_name: str = "") -> str:
    """Decide the label prefix from job + session_name hints.

    Claude Code reports its jobName as 'Python' or 'claude.exe' depending
    on platform, but its session_name typically contains 'Claude Code'.
    Codex is more honest and reports 'codex'. We check both fields.
    """
    job_haystack = (job or "").lower()
    if "codex" in job_haystack:
        return "codex"
    if "claude" in job_haystack:
        return "claude"
    name_haystack = (session_name or "").lower()
    if "codex" in name_haystack:
        return "codex"
    if "claude" in name_haystack:
        return "claude"
    return "agent"


def _next_auto_label(job: str, session_name: str = "") -> str:
    """Pick the next free ``{base}{n}`` label."""
    base = _classify(job, session_name)
    used = set(registry.all_labels().keys())
    n = 1
    while f"{base}{n}" in used:
        n += 1
    return f"{base}{n}"


def _auto_register_from_env() -> None:
    """Attach a label to the calling pane on server startup.

    Order:
      1. If ``TEAMMATE_LABEL`` is exported, use it verbatim.
      2. Otherwise auto-assign the next free ``{job}{n}`` slot
         (``claude1``, ``codex1``, ``codex2`` …).

    Non-fatal: if iTerm's Python API isn't reachable we silently skip.
    """
    explicit = os.environ.get("TEAMMATE_LABEL", "").strip()
    tsid = os.environ.get("TERM_SESSION_ID", "")
    sid_tail = tsid.split(":", 1)[1] if ":" in tsid else tsid
    if not sid_tail:
        return

    async def _go():
        try:
            connection = await iterm2.Connection.async_create()
        except Exception:
            return
        try:
            from .iterm import list_sessions
            refs = await list_sessions(connection)
            me = None
            for r in refs:
                if r.session_id.upper().endswith(sid_tail.upper()):
                    me = r
                    break
            if me is None:
                return
            # Reuse an existing label for this pane if there is one.
            existing_label = next(
                (l for l, r in registry.all_labels().items()
                 if (r.get("session_id") or "").upper() == me.session_id.upper()),
                None,
            )
            label = explicit or existing_label or _next_auto_label(me.job, me.name or "")
            _register_via_daemon_or_direct(
                label=label,
                session_id=me.session_id,
                pid=os.getpid(),
                job=me.job,
                cwd=me.cwd,
                extra={"session_name": me.name or None,
                       "auto_assigned": not explicit},
            )
            # Make the chosen label visible to the *current* server
            # process — used as `from_agent` in queue records.
            os.environ["TEAMMATE_LABEL"] = label
            _log.event("auto_register", label=label,
                       session_id=me.session_id, auto=not explicit)
        finally:
            try:
                connection.close()
            except Exception:
                pass

    try:
        asyncio.get_event_loop().run_until_complete(_go())
    except RuntimeError:
        asyncio.run(_go())
    except Exception:
        pass


def main():
    """Entry point used by `teammate-mcp` console script."""
    _log.event("server.start", queue_mode=QUEUE_MODE, cwd=PROJECT_CWD)
    _auto_register_from_env()
    mcp.run()


if __name__ == "__main__":
    main()
