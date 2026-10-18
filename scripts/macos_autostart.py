#!/usr/bin/env python3
"""Generate the local Saturday LaunchAgent plist."""

import plistlib
import sys
from pathlib import Path


def launch_agent(repo_dir: str, label: str, *, action: str = "start") -> dict:
    if action == "start":
        script = "open_keiba.sh"
        hour, minute = 9, 40
    elif action == "stop":
        script = "close_keiba.sh"
        hour, minute = 16, 30
    else:
        raise ValueError(f"unknown LaunchAgent action: {action}")
    return {
        "Label": label,
        "ProgramArguments": [str(Path(repo_dir) / "scripts" / script)],
        # launchd uses 0/7 for Sunday, so Saturday is 6.
        "StartCalendarInterval": {"Weekday": 6, "Hour": hour, "Minute": minute},
        # The wrapper exits after opening the browser; keep make dev alive.
        "AbandonProcessGroup": action == "start",
        "RunAtLoad": False,
        "StandardOutPath": "/tmp/horse-race-saturday.log",
        "StandardErrorPath": "/tmp/horse-race-saturday.log",
    }


if __name__ == "__main__":
    repo_dir, plist_path, label, action = sys.argv[1:]
    with open(plist_path, "wb") as output:
        plistlib.dump(launch_agent(repo_dir, label, action=action), output)
