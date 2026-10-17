#!/usr/bin/env python3
"""Generate the local Saturday LaunchAgent plist."""

import plistlib
import sys
from pathlib import Path


def launch_agent(repo_dir: str, label: str) -> dict:
    return {
        "Label": label,
        "ProgramArguments": [str(Path(repo_dir) / "scripts" / "open_keiba.sh")],
        # launchd uses 0/7 for Sunday, so Saturday is 6.
        "StartCalendarInterval": {"Weekday": 6, "Hour": 9, "Minute": 40},
        # The wrapper exits after opening the browser; keep make dev alive.
        "AbandonProcessGroup": True,
        "RunAtLoad": False,
        "StandardOutPath": "/tmp/horse-race-saturday.log",
        "StandardErrorPath": "/tmp/horse-race-saturday.log",
    }


if __name__ == "__main__":
    repo_dir, plist_path, label = sys.argv[1:]
    with open(plist_path, "wb") as output:
        plistlib.dump(launch_agent(repo_dir, label), output)
