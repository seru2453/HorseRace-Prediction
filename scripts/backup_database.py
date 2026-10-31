#!/usr/bin/env python3
"""Write a verified, atomic JSON backup using the application's export format."""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.repository import RaceRepository
from database.session import get_session_factory


DEFAULT_BACKUP_DIR = ROOT / "data" / "backups"
MAX_BACKUPS = 20
BACKUP_PATTERN = re.compile(r"^horseprediction-\d{4}-\d{2}-\d{2}\.json$")


def create_backup(
    database_url: str | None = None,
    backup_dir: Path = DEFAULT_BACKUP_DIR,
    backup_date: date | None = None,
) -> tuple[Path, dict[str, int]]:
    with get_session_factory(database_url)() as session:
        payload = RaceRepository(session).export_data()

    if payload.get("format_version") != RaceRepository.EXPORT_FORMAT_VERSION:
        raise ValueError("Export format version did not match the application import format.")
    expected_counts = payload.get("counts")
    if not isinstance(expected_counts, dict) or not isinstance(payload.get("data"), dict):
        raise ValueError("Export payload is missing its counts or data.")

    backup_dir.mkdir(parents=True, exist_ok=True)
    local_date = backup_date or datetime.now(ZoneInfo("Asia/Tokyo")).date()
    target = backup_dir / f"horseprediction-{local_date.isoformat()}.json"
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=backup_dir, prefix=f".{target.name}.", suffix=".tmp", delete=False
        ) as output:
            temporary_path = Path(output.name)
            json.dump(payload, output, ensure_ascii=False, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())

        with temporary_path.open(encoding="utf-8") as source:
            verified = json.load(source)
        if verified.get("format_version") != RaceRepository.EXPORT_FORMAT_VERSION:
            raise ValueError("Written backup failed format-version verification.")
        if verified.get("counts") != expected_counts:
            raise ValueError("Written backup failed record-count verification.")
        temporary_path.replace(target)
        temporary_path = None
        _prune_backups(backup_dir)
        return target, expected_counts
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _prune_backups(backup_dir: Path) -> None:
    backups = sorted(
        (path for path in backup_dir.iterdir() if BACKUP_PATTERN.fullmatch(path.name)),
        key=lambda path: path.name,
        reverse=True,
    )
    for old_backup in backups[MAX_BACKUPS:]:
        old_backup.unlink()


if __name__ == "__main__":
    backup_path, record_counts = create_backup(os.getenv("DATABASE_URL"))
    print(f"Backup verified: {backup_path} counts={json.dumps(record_counts, ensure_ascii=False, sort_keys=True)}", flush=True)
