#!/usr/bin/env bash
# Installs the LaunchAgent plist for Ion. It does NOT load it; you decide when.
#   deploy/install.sh [path/to/config.yml]
set -euo pipefail
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${1:-$HOME/.ion/ion.yml}"
UV="${UV:-$(command -v uv || echo "$HOME/.local/bin/uv")}"
LABEL="ai.aska.ion"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

[ -f "$CONFIG" ] || { echo "config not found: $CONFIG" >&2; exit 1; }
[ -x "$UV" ] || { echo "uv not found (set UV=/path/to/uv)" >&2; exit 1; }

(cd "$SRC_DIR" && "$UV" sync --no-dev --extra graph >/dev/null)
ION_BIN="$SRC_DIR/.venv/bin/ion"
[ -x "$ION_BIN" ] || { echo "ion entry point missing at $ION_BIN" >&2; exit 1; }

mkdir -p "$HOME/.ion" "$HOME/Library/LaunchAgents"
sed -e "s|__ION_BIN__|$ION_BIN|g" -e "s|__CONFIG__|$CONFIG|g" \
    -e "s|__SRC_DIR__|$SRC_DIR|g" -e "s|__HOME__|$HOME|g" \
    "$SRC_DIR/deploy/ai.aska.ion.plist" > "$DEST"
plutil -lint "$DEST"

echo "installed $DEST (not loaded)."
echo "check first:   $ION_BIN --config $CONFIG doctor"
echo "to enable:     launchctl bootstrap gui/$(id -u) $DEST"
echo "to disable:    launchctl bootout gui/$(id -u)/$LABEL"
echo "kill switch:   $ION_BIN pause   (and resume)"
