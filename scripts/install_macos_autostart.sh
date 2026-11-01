#!/bin/bash

set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This installer supports macOS only." >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
PLIST_DIR="$HOME/Library/LaunchAgents"
mkdir -p "$PLIST_DIR"

ACTIONS=(start stop catchup collect collect_late backup train)
PLIST_PATHS=()
for action in "${ACTIONS[@]}"; do
  case "$action" in
    start) label="com.horserace.saturday" ;;
    stop) label="com.horserace.saturday-stop" ;;
    *) label="com.horserace.weekend-$action" ;;
  esac
  plist_path="$PLIST_DIR/$label.plist"
  python3 "$REPO_DIR/scripts/macos_autostart.py" "$REPO_DIR" "$plist_path" "$label" "$action"
  PLIST_PATHS+=("$plist_path")
done

for plist_path in "${PLIST_PATHS[@]}"; do
  launchctl bootout "gui/$(id -u)" "$plist_path" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "$plist_path"
done
echo "Installed weekend app startup, catch-up, result collection, backup, and Sunday Challenger training LaunchAgents."
echo "Run scripts/open_keiba.sh to try it now."
