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

            # Visibility shotgun — set the label everywhere iTerm can
            # render it. The user has many possible viewing surfaces
            # (Profile-dependent), so we just hit all of them and at
            # least one will be visible.
            import base64 as _b64
            badge_b64 = _b64.b64encode(label.encode()).decode()
            esc = "".join([
                f"\x1b]0;[{label}]\x07",     # window title (top of window)
                f"\x1b]1;[{label}]\x07",     # icon name
                f"\x1b]2;[{label}]\x07",     # tab title
                f"\x1b]1337;SetBadgeFormat={badge_b64}\x07",       # badge
                f"\x1b]1337;SetUserVar=teammate_label={badge_b64}\x07",  # user var (status bar component)
            ])
            try:
                await me.session.async_send_text(esc)
            except Exception:
                pass
            sys.stdout.write(esc)
            sys.stdout.flush()

            # In-pane visual banner — large coloured line that the user
            # sees in their scrollback even after the CLI takes over.
            banner = (
                f"\x1b[1;7;36m"   # bold + reverse + cyan
                f"  ▌ teammate-mcp ▌  this pane = {label}  "
                f"\x1b[0m\n"
            )
            sys.stdout.write(banner)

            print(f"✓ registered as {label}  (session {me.session_id[:8]}…, "
                  f"job={me.job!r}, cwd={me.cwd})")
            print(f"  → tab title, window title, and iTerm badge set to "
                  f"[{label}]")
            print(f"  → if nothing is visible: iTerm Preferences → "
                  f"Profile → General → Badge (enable + bright color), "
                  f"or Appearance → Panes → 'Show titles in tabs / panes'")
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
                "Guid": "TEAMMATE-MCP-PROFILE-V1",
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
    if cmd == "register-pane":
        sys.exit(_cmd_register_pane(rest))
    if cmd == "list":
        sys.exit(_cmd_list())
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
