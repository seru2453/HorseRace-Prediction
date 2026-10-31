import sqlite3
import shutil
from datetime import date, timedelta

import pytest

from database.repository import RaceRepository
from database.session import make_session_factory
from scripts.backup_database import MAX_BACKUPS, create_backup
from services.app.orchestrator import AnalysisRequest, RaceAnalysisService
from services.data_provider.mock import MockProvider


def test_sqlite_online_backup_is_consistent_and_prunes_old_generations(tmp_path):
    source_database = tmp_path / "source.sqlite3"
    database_url = f"sqlite:///{source_database}"
    RaceAnalysisService(MockProvider(), database_url).analyze(
        AnalysisRequest("2026-08-16", "札幌", 11, iterations=1000, seed=123)
    )
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    for offset in range(MAX_BACKUPS + 2, 0, -1):
        old_date = date(2026, 8, 16) - timedelta(days=offset)
        (backup_dir / f"horseprediction-{old_date.isoformat()}.sqlite3").write_bytes(b"old backup")
    unrelated = backup_dir / "notes.txt"
    unrelated.write_text("keep", encoding="utf-8")

    backup_path, counts = create_backup(database_url, backup_dir, date(2026, 8, 16))

    with sqlite3.connect(backup_path) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT COUNT(*) FROM races").fetchone()[0] == 1
    assert backup_path.suffix == ".sqlite3"
    assert counts["races"] == 1
    assert not list(backup_dir.glob("*.tmp"))
    assert unrelated.read_text(encoding="utf-8") == "keep"
    backups = sorted(backup_dir.glob("horseprediction-*.sqlite3"))
    assert len(backups) == MAX_BACKUPS
    assert backups[-1] == backup_path

    target_database = tmp_path / "restored.sqlite3"
    with sqlite3.connect(backup_path) as source, sqlite3.connect(target_database) as destination:
        source.backup(destination)
    with make_session_factory(f"sqlite:///{target_database}")() as session:
        assert RaceRepository(session).race_detail("20260816-sapporo-11") is not None


def test_backup_failure_preserves_existing_backup_and_cleans_temp(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path}/backup-failure.sqlite3"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    existing = backup_dir / "horseprediction-2026-08-15.sqlite3"
    existing.write_bytes(b"previous verified backup")

    monkeypatch.setattr("scripts.backup_database._database_path", lambda _url: tmp_path / "missing.sqlite3")
    with pytest.raises(FileNotFoundError, match="does not exist"):
        create_backup(database_url, backup_dir, date(2026, 8, 16))

    assert existing.read_bytes() == b"previous verified backup"
    assert not list(backup_dir.glob("*.tmp"))
