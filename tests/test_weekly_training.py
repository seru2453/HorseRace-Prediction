from datetime import datetime
from zoneinfo import ZoneInfo

from database.models import TrainingRun
from database.session import make_session_factory
from sqlalchemy import select

from apps.cli.weekly_training import train_weekly
from services.app.orchestrator import AnalysisRequest, RaceAnalysisService
from services.data_provider.mock import MockProvider


def test_weekly_training_persists_once_and_marks_small_sample_reference_only(tmp_path):
    database_url = f"sqlite:///{tmp_path}/weekly.sqlite3"
    now = datetime(2026, 11, 1, 18, 0, tzinfo=ZoneInfo("Asia/Tokyo"))

    first = train_weekly(now, database_url, tmp_path / "models")
    second = train_weekly(now, database_url, tmp_path / "models")

    assert first["status"] == "insufficient_data"
    assert first["sample_status"] == "sample_small"
    assert first["application_status"] == "reference_only"
    assert first["challenger_only"] is True
    assert first["promotion_status"] == "not_promoted"
    assert second["status"] == "already_trained"
    with make_session_factory(database_url)() as session:
        runs = session.scalars(select(TrainingRun)).all()
    assert len(runs) == 1
    assert runs[0].metrics_json["scheduled_week"] == "2026-W44"


def test_weekly_training_defers_while_a_saved_prediction_lacks_review(tmp_path):
    database_url = f"sqlite:///{tmp_path}/pending.sqlite3"
    RaceAnalysisService(MockProvider(), database_url).analyze(
        AnalysisRequest("2026-08-16", "札幌", 11, iterations=1000)
    )

    result = train_weekly(
        datetime(2026, 8, 16, 18, 0, tzinfo=ZoneInfo("Asia/Tokyo")),
        database_url,
        tmp_path / "models",
    )

    assert result["status"] == "deferred_pending_results"
    assert result["pending_race_count"] == 1
    with make_session_factory(database_url)() as session:
        assert session.scalars(select(TrainingRun)).all() == []
