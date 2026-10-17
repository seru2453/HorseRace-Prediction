#!/bin/bash

set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This installer supports macOS only." >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.horserace.saturday"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/$LABEL.plist"

mkdir -p "$PLIST_DIR"
python3 - "$REPO_DIR" "$PLIST_PATH" "$LABEL" <<'PY'
import plistlib
import sys
from pathlib import Path

repo_dir, plist_path, label = sys.argv[1:]
schedule = {
    "Label": label,
    "ProgramArguments": [str(Path(repo_dir) / "scripts" / "open_keiba.sh")],
    "StartCalendarInterval": {"Weekday": 7, "Hour": 9, "Minute": 40},
    "RunAtLoad": False,
    "StandardOutPath": "/tmp/horse-race-saturday.log",
    "StandardErrorPath": "/tmp/horse-race-saturday.log",
}
with open(plist_path, "wb") as output:
    plistlib.dump(schedule, output)
PY

launchctl bootout "gui/$(id -u)" "$PLIST_PATH" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
echo "Installed $PLIST_PATH (Saturdays at 09:40)."
echo "Run scripts/open_keiba.sh to try it now."
