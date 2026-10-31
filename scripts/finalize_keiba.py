#!/usr/bin/env python3
"""Run one bounded final data pass, then safely stop this repository's app."""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


REPO_DIR = Path(__file__).resolve().parents[1]
LOG_PATH = Path("/tmp/horse-race-saturday.log")
PIPELINE_TIMEOUT_SECONDS = 900
CLOSE_TIMEOUT_SECONDS = 35


def log(message: str) -> None:
    timestamp = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d %H:%M:%S %Z")
    with LOG_PATH.open("a", encoding="utf-8") as output:
        output.write(f"[{timestamp}] {message}\n")


def finalize(repo_dir: Path = REPO_DIR, runner=subprocess.run) -> int:
    pipeline = [sys.executable, str(repo_dir / "apps/cli/daily_pipeline.py")]
    close = [str(repo_dir / "scripts/close_keiba.sh")]
    pipeline_status = 0
    try:
        log("Starting final daily pipeline before app shutdown.")
        result = runner(pipeline, cwd=repo_dir, timeout=PIPELINE_TIMEOUT_SECONDS, check=False)
        pipeline_status = result.returncode
        log(f"Final daily pipeline finished with status {pipeline_status}.")
    except subprocess.TimeoutExpired:
        pipeline_status = 124
        log(f"Final daily pipeline timed out after {PIPELINE_TIMEOUT_SECONDS}s; continuing shutdown.")
    except Exception as exc:  # Keep shutdown independent of pipeline health.
        pipeline_status = 1
        log(f"Final daily pipeline failed: {exc}; continuing shutdown.")

    try:
        result = runner(close, cwd=repo_dir, timeout=CLOSE_TIMEOUT_SECONDS, check=False)
        if result.returncode:
            log(f"App shutdown returned status {result.returncode}.")
            return result.returncode
    except Exception as exc:
        log(f"App shutdown failed: {exc}.")
        return 1
    return pipeline_status


if __name__ == "__main__":
    raise SystemExit(finalize())
