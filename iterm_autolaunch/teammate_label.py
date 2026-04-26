#!/usr/bin/env python3
"""teammate-mcp iTerm Status Bar component.

AutoLaunched by iTerm2 on startup. Registers a StatusBarComponent that
reads ~/.teammate-mcp/registry.json and shows the label assigned to
each pane (`[codex1]`, `[claude1]`, ...) on its bottom status bar.

Drop into:
    ~/Library/Application Support/iTerm2/Scripts/AutoLaunch/

The matching component then appears in:
    Settings → Profiles → Session → Configure Status Bar
under the name "teammate label". Drag it to Active Components.

Once added, every pane registered via `teammate-mcp register-pane`
shows its label on the iTerm status bar — Codex panes finally get
the same surface Claude Code's statusLine offers.
"""

import asyncio
import json
import sys
from pathlib import Path

import iterm2

REGISTRY = Path.home() / ".teammate-mcp" / "registry.json"


def _read_registry() -> dict:
    try:
        return json.loads(REGISTRY.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


async def main(connection):
    component = iterm2.StatusBarComponent(
        short_description="teammate label",
        detailed_description="Shows the teammate-mcp pane label "
                              "registered for this session.",
        knobs=[],
        exemplar="[codex1]",
        update_cadence=2,
        identifier="com.teammate.label",
    )

    @iterm2.StatusBarRPC
    async def teammate_label_provider(
        knobs,
        session_id=iterm2.Reference("id"),
    ):
        if not session_id:
            return ""
        sid_up = str(session_id).upper()
        reg = _read_registry()
        for label, rec in reg.items():
            rec_sid = (rec.get("session_id") or "").upper()
            if rec_sid == sid_up or rec_sid.endswith(sid_up) or sid_up.endswith(rec_sid):
                return f"[{label}]"
        return ""  # unregistered panes show nothing — keeps zsh quiet

    await component.async_register(connection, teammate_label_provider)


if __name__ == "__main__":
    iterm2.run_forever(main)
