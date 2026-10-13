#!/usr/bin/env bash
# Installs the LaunchAgent plist for Mimar. It does NOT load it; you decide when.
#   deploy/install.sh [path/to/config.yml]
set -euo pipefail
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${1:-$SRC_DIR/config/aska-digital.yml}"
UV="${UV:-$(command -v uv || echo "$HOME/.local/bin/uv")}"
LABEL="ai.aska.mimar"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

[ -f "$CONFIG" ] || { echo "config not found: $CONFIG" >&2; exit 1; }
[ -x "$UV" ] || { echo "uv not found (set UV=/path/to/uv)" >&2; exit 1; }

(cd "$SRC_DIR" && "$UV" sync --no-dev >/dev/null)
MIMAR_BIN="$SRC_DIR/.venv/bin/mimar"
[ -x "$MIMAR_BIN" ] || { echo "mimar entry point missing at $MIMAR_BIN" >&2; exit 1; }

mkdir -p "$HOME/.mimar" "$HOME/Library/LaunchAgents"
sed -e "s|__MIMAR_BIN__|$MIMAR_BIN|g" -e "s|__CONFIG__|$CONFIG|g" \
    -e "s|__SRC_DIR__|$SRC_DIR|g" -e "s|__HOME__|$HOME|g" \
    "$SRC_DIR/deploy/ai.aska.mimar.plist" > "$DEST"
plutil -lint "$DEST"

echo "installed $DEST (not loaded)."
echo "check first:   $MIMAR_BIN --config $CONFIG doctor"
echo "to enable:     launchctl bootstrap gui/$(id -u) $DEST"
echo "to disable:    launchctl bootout gui/$(id -u)/$LABEL"
echo "kill switch:   $MIMAR_BIN pause   (and resume)"
