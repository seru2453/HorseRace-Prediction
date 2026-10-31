#!/usr/bin/env python3
"""Create a verified, atomic SQLite online backup."""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from zoneinfo import ZoneInfo

from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.session import get_session_factory

DEFAULT_BACKUP_DIR = ROOT / "data" / "backups"
MAX_BACKUPS = 20
BACKUP_PATTERN = re.compile(r"^horseprediction-\d{4}-\d{2}-\d{2}\.sqlite3$")


def _database_path(database_url: str | None) -> Path:
    factory = get_session_factory(database_url)
    url = factory.kw["bind"].url
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        raise ValueError("SQLite file database is required for online backup.")
    path = Path(url.database)
    return path if path.is_absolute() else (Path.cwd() / path)


def _table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    tables = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    counts = {}
    for (table_name,) in tables:
        quoted_name = table_name.replace('"', '""')
        counts[table_name] = int(connection.execute(f'SELECT COUNT(*) FROM "{quoted_name}"').fetchone()[0])
    return counts


def create_backup(
    database_url: str | None = None,
    backup_dir: Path = DEFAULT_BACKUP_DIR,
    backup_date: date | None = None,
) -> tuple[Path, dict[str, int]]:
    source_path = _database_path(database_url)
    if not source_path.is_file():
        raise FileNotFoundError(f"SQLite database does not exist: {source_path}")

    backup_dir.mkdir(parents=True, exist_ok=True)
    local_date = backup_date or datetime.now(ZoneInfo("Asia/Tokyo")).date()
    target = backup_dir / f"horseprediction-{local_date.isoformat()}.sqlite3"
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(dir=backup_dir, prefix=f".{target.name}.", suffix=".tmp", delete=False) as temporary:
            temporary_path = Path(temporary.name)
        with closing(sqlite3.connect(f"{source_path.as_uri()}?mode=ro", uri=True)) as source, closing(
            sqlite3.connect(temporary_path)
        ) as destination:
            source.backup(destination, pages=256)
            check = destination.execute("PRAGMA quick_check").fetchone()
            if not check or check[0] != "ok":
                raise ValueError(f"SQLite backup integrity check failed: {check}")
            record_counts = _table_counts(destination)
        with temporary_path.open("rb") as backup_file:
            os.fsync(backup_file.fileno())
        temporary_path.replace(target)
        temporary_path = None
        _prune_backups(backup_dir)
        return target, record_counts
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
    print(f"SQLite backup verified: {backup_path} counts={record_counts}", flush=True)
