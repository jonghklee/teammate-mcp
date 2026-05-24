#!/usr/bin/env bash
# Install / update / uninstall the teammate-mcp daemon launchd plist.
#
# Usage:
#   ./install.sh            # install (copy + bootstrap)
#   ./install.sh --reload   # reinstall (unload + reload — picks up plist changes)
#   ./install.sh --uninstall
#   ./install.sh --status

set -e

PLIST_NAME="com.teammate-mcp.daemon"
PLIST_SRC="$(cd "$(dirname "$0")" && pwd)/${PLIST_NAME}.plist"
LA_DIR="${HOME}/Library/LaunchAgents"
LA_PATH="${LA_DIR}/${PLIST_NAME}.plist"

case "${1:-install}" in
  install)
    mkdir -p "$LA_DIR"
    cp "$PLIST_SRC" "$LA_PATH"
    # launchctl bootstrap is the modern API (replaces `load`).
    launchctl bootstrap "gui/$(id -u)" "$LA_PATH" 2>/dev/null || {
      # If already loaded, that's fine. Otherwise show the error.
      if launchctl print "gui/$(id -u)/${PLIST_NAME}" >/dev/null 2>&1; then
        echo "already loaded: ${PLIST_NAME}"
      else
        echo "ERROR: launchctl bootstrap failed" >&2
        exit 1
      fi
    }
    echo "✓ installed: $LA_PATH"
    echo "✓ launched: ${PLIST_NAME}"
    sleep 1
    launchctl print "gui/$(id -u)/${PLIST_NAME}" 2>/dev/null | grep -E "state|pid" | head -5
    ;;

  --reload|reload)
    if [ -f "$LA_PATH" ]; then
      launchctl bootout "gui/$(id -u)/${PLIST_NAME}" 2>/dev/null || true
    fi
    cp "$PLIST_SRC" "$LA_PATH"
    launchctl bootstrap "gui/$(id -u)" "$LA_PATH"
    echo "✓ reloaded: ${PLIST_NAME}"
    ;;

  --uninstall|uninstall)
    launchctl bootout "gui/$(id -u)/${PLIST_NAME}" 2>/dev/null || true
    rm -f "$LA_PATH"
    echo "✓ uninstalled: ${PLIST_NAME}"
    ;;

  --status|status)
    if launchctl print "gui/$(id -u)/${PLIST_NAME}" >/dev/null 2>&1; then
      echo "✓ loaded"
      launchctl print "gui/$(id -u)/${PLIST_NAME}" | grep -E "state|pid|last exit code" | head -5
    else
      echo "✗ not loaded — run ./install.sh to install"
    fi
    ;;

  *)
    echo "usage: $0 [install|--reload|--uninstall|--status]" >&2
    exit 2
    ;;
esac
