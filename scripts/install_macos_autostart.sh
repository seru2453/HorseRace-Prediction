#!/bin/bash

set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This installer supports macOS only." >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
LABEL="com.horserace.saturday"
STOP_LABEL="com.horserace.saturday-stop"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/$LABEL.plist"
STOP_PLIST_PATH="$PLIST_DIR/$STOP_LABEL.plist"

mkdir -p "$PLIST_DIR"
python3 "$REPO_DIR/scripts/macos_autostart.py" "$REPO_DIR" "$PLIST_PATH" "$LABEL" start
python3 "$REPO_DIR/scripts/macos_autostart.py" "$REPO_DIR" "$STOP_PLIST_PATH" "$STOP_LABEL" stop

for plist_path in "$PLIST_PATH" "$STOP_PLIST_PATH"; do
  launchctl bootout "gui/$(id -u)" "$plist_path" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "$plist_path"
done
echo "Installed Saturday startup at 09:40: $PLIST_PATH"
echo "Installed Saturday shutdown at 16:30: $STOP_PLIST_PATH"
echo "Run scripts/open_keiba.sh to try it now."
