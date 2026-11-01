#!/usr/bin/env python3
"""Generate the local weekend LaunchAgent plist."""

import plistlib
import sys
from pathlib import Path


def launch_agent(repo_dir: str, label: str, *, action: str = "start") -> dict:
    weekdays = (6, 0)
    if action == "start":
        script = "open_keiba.sh"
        hour, minute = 9, 40
    elif action == "stop":
        script = "finalize_keiba.py"
        hour, minute = 16, 30
    elif action == "catchup":
        script = "weekend_catchup.py"
        hour = minute = None
        weekdays = None
    elif action == "collect":
        script = "daily_pipeline.py"
        hour, minute = 17, 0
    elif action == "collect_late":
        script = "daily_pipeline.py"
        hour, minute = 17, 30
    elif action == "backup":
        script = "backup_database.py"
        hour, minute = 17, 35
    elif action == "train":
        script = "weekly_training.py"
        hour, minute = 18, 0
        weekdays = (0,)
    else:
        raise ValueError(f"unknown LaunchAgent action: {action}")
    if action in {"collect", "collect_late"}:
        arguments = [sys.executable, str(Path(repo_dir) / "apps" / "cli" / script), "--results-only"]
    elif action == "train":
        arguments = [sys.executable, str(Path(repo_dir) / "apps" / "cli" / script)]
    elif action in {"stop", "catchup", "backup"}:
        arguments = [sys.executable, str(Path(repo_dir) / "scripts" / script)]
    else:
        arguments = [str(Path(repo_dir) / "scripts" / script)]
    agent = {
        "Label": label,
        "ProgramArguments": arguments,
        # The wrapper exits after opening the browser; keep make dev alive.
        "AbandonProcessGroup": action == "start",
        "RunAtLoad": action == "catchup",
        "WorkingDirectory": repo_dir,
        "StandardOutPath": "/tmp/horse-race-saturday.log",
        "StandardErrorPath": "/tmp/horse-race-saturday.log",
    }
    if weekdays:
        # launchd uses 0/7 for Sunday and 6 for Saturday.
        agent["StartCalendarInterval"] = [
            {"Weekday": weekday, "Hour": hour, "Minute": minute} for weekday in weekdays
        ]
    return agent


if __name__ == "__main__":
    repo_dir, plist_path, label, action = sys.argv[1:]
    with open(plist_path, "wb") as output:
        plistlib.dump(launch_agent(repo_dir, label, action=action), output)
