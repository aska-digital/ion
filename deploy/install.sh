#!/usr/bin/env bash
# Installs the LaunchAgent plist for Ion. It does NOT load it; you decide when.
#   deploy/install.sh [path/to/config.yml]
# The agent gets the same state directory (ION_HOME, default ~/.ion) and config (the argument, else ION_CONFIG, else
# <state dir>/ion.yml) as this shell, so `ion pause` here stops the scheduled run too.
set -euo pipefail
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="${ION_HOME:-$HOME/.ion}"
CONFIG="${1:-${ION_CONFIG:-$STATE_DIR/ion.yml}}"
UV="${UV:-$(command -v uv || echo "$HOME/.local/bin/uv")}"
LABEL="ai.aska.ion"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ -z "${ION_HOME:-}" ] && [ -f "$HOME/.mimar/state.db" ] && [ ! -f "$STATE_DIR/state.db" ]; then
  echo "ion state is still in ~/.mimar (the old name). Move it first, or the agent starts from empty state" >&2
  echo "and ignores ~/.mimar/PAUSED:  mkdir -p ~/.ion && mv ~/.mimar/* ~/.ion/" >&2
  exit 1
fi
[ -f "$CONFIG" ] || { echo "config not found: $CONFIG" >&2; exit 1; }
[ -x "$UV" ] || { echo "uv not found (set UV=/path/to/uv)" >&2; exit 1; }
# launchd runs the agent in $SRC_DIR, so a relative path would name a different file there
CONFIG="$(cd "$(dirname "$CONFIG")" && pwd)/$(basename "$CONFIG")"
STATE_DIR="$(mkdir -p "$STATE_DIR" && cd "$STATE_DIR" && pwd)"

(cd "$SRC_DIR" && "$UV" sync --locked --no-dev >/dev/null)
ION_BIN="$SRC_DIR/.venv/bin/ion"
[ -x "$ION_BIN" ] || { echo "ion entry point missing at $ION_BIN" >&2; exit 1; }

mkdir -p "$HOME/Library/LaunchAgents"
esc() { printf '%s' "$1" | sed -e 's/[&|\\]/\\&/g'; }
sed -e "s|__ION_BIN__|$(esc "$ION_BIN")|g" -e "s|__CONFIG__|$(esc "$CONFIG")|g" \
    -e "s|__SRC_DIR__|$(esc "$SRC_DIR")|g" -e "s|__ION_HOME__|$(esc "$STATE_DIR")|g" -e "s|__HOME__|$(esc "$HOME")|g" \
    "$SRC_DIR/deploy/ai.aska.ion.plist" > "$DEST"
plutil -lint "$DEST"

echo "installed $DEST (not loaded)."
echo "check first:   ION_HOME=$STATE_DIR $ION_BIN --config $CONFIG doctor"
echo "to enable:     launchctl bootstrap gui/$(id -u) $DEST"
echo "to disable:    launchctl bootout gui/$(id -u)/$LABEL"
echo "kill switch:   ION_HOME=$STATE_DIR $ION_BIN pause   (and resume)"
