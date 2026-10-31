import json
from datetime import date, timedelta

import pytest

from database.repository import RaceRepository
from database.session import make_session_factory
from scripts.backup_database import MAX_BACKUPS, create_backup
from services.app.orchestrator import AnalysisRequest, RaceAnalysisService
from services.data_provider.mock import MockProvider


def test_backup_is_import_compatible_and_prunes_old_generations(tmp_path):
    source_database = f"sqlite:///{tmp_path}/source.sqlite3"
    RaceAnalysisService(MockProvider(), source_database).analyze(
        AnalysisRequest("2026-08-16", "札幌", 11, iterations=1000, seed=123)
    )
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    for offset in range(MAX_BACKUPS + 2, 0, -1):
        old_date = date(2026, 8, 16) - timedelta(days=offset)
        (backup_dir / f"horseprediction-{old_date.isoformat()}.json").write_text("{}", encoding="utf-8")
    unrelated = backup_dir / "notes.txt"
    unrelated.write_text("keep", encoding="utf-8")

    backup_path, counts = create_backup(source_database, backup_dir, date(2026, 8, 16))

    payload = json.loads(backup_path.read_text(encoding="utf-8"))
    assert payload["format_version"] == RaceRepository.EXPORT_FORMAT_VERSION
    assert counts["races"] == 1
    assert payload["counts"] == counts
    assert not list(backup_dir.glob("*.tmp"))
    assert unrelated.read_text(encoding="utf-8") == "keep"
    backups = sorted(backup_dir.glob("horseprediction-*.json"))
    assert len(backups) == MAX_BACKUPS
    assert backups[-1] == backup_path

    target_database = f"sqlite:///{tmp_path}/restored.sqlite3"
    with make_session_factory(target_database)() as session:
        imported = RaceRepository(session).import_data(payload)
        assert imported["imported"]["races"] == 1
        assert RaceRepository(session).race_detail("20260816-sapporo-11") is not None


def test_export_failure_preserves_existing_backup_and_cleans_temp(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path}/backup-failure.sqlite3"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    existing = backup_dir / "horseprediction-2026-08-15.json"
    existing.write_text("previous verified backup", encoding="utf-8")

    def fail_export(_self):
        raise RuntimeError("export failed")

    monkeypatch.setattr(RaceRepository, "export_data", fail_export)
    with pytest.raises(RuntimeError, match="export failed"):
        create_backup(database_url, backup_dir, date(2026, 8, 16))

    assert existing.read_text(encoding="utf-8") == "previous verified backup"
    assert not list(backup_dir.glob("*.tmp"))
