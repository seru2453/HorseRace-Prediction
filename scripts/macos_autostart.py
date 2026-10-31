#!/usr/bin/env python3
"""Generate the local weekend LaunchAgent plist."""

import plistlib
import sys
from pathlib import Path


def launch_agent(repo_dir: str, label: str, *, action: str = "start") -> dict:
    if action == "start":
        script = "open_keiba.sh"
        hour, minute = 9, 40
    elif action == "stop":
        script = "finalize_keiba.py"
        hour, minute = 16, 30
    else:
        raise ValueError(f"unknown LaunchAgent action: {action}")
    return {
        "Label": label,
        "ProgramArguments": (
            [sys.executable, str(Path(repo_dir) / "scripts" / script)]
            if action == "stop"
            else [str(Path(repo_dir) / "scripts" / script)]
        ),
        # launchd uses 0/7 for Sunday and 6 for Saturday.
        "StartCalendarInterval": [
            {"Weekday": weekday, "Hour": hour, "Minute": minute}
            for weekday in (6, 0)
        ],
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
