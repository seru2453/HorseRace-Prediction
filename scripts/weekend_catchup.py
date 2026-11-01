#!/usr/bin/env python3
"""Start the app on login only during the active weekend prediction window."""

from datetime import datetime, time
from pathlib import Path
import subprocess
from zoneinfo import ZoneInfo


def should_catch_up(now: datetime) -> bool:
    local = now.astimezone(ZoneInfo("Asia/Tokyo"))
    return local.weekday() in (5, 6) and time(9, 40) <= local.time() <= time(16, 30)


def main() -> int:
    if not should_catch_up(datetime.now(ZoneInfo("Asia/Tokyo"))):
        return 0
    repo_dir = Path(__file__).resolve().parents[1]
    return subprocess.run([str(repo_dir / "scripts" / "open_keiba.sh")], cwd=repo_dir, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
