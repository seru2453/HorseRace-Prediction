from datetime import datetime
from zoneinfo import ZoneInfo

from database.repository import RaceRepository
from database.session import make_session_factory
from services.app.orchestrator import AnalysisRequest, RaceAnalysisService
from services.data_provider.base import ProviderUnavailableError
from services.data_provider.mock import MockProvider
from services.common.models import RaceScheduleItem
from services.learning.daily_pipeline import DailyRacePipeline


def test_daily_pipeline_predicts_once_before_start(tmp_path):
    database_url = f"sqlite:///{tmp_path}/pipeline.sqlite3"
    provider = MockProvider()
    service = DailyRacePipeline(provider, database_url)

    before = datetime(2026, 8, 16, 14, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
    first = service.run_once(now=before, prediction_lead_minutes=120)
    second = service.run_once(now=before, prediction_lead_minutes=120)

    assert first["predictions"][0]["status"] == "predicted"
    assert second["predictions"][0]["status"] == "already_predicted"
    with make_session_factory(database_url)() as session:
        assert RaceRepository(session).has_prediction_snapshot("20260816-sapporo-11")


def test_daily_pipeline_does_not_create_prediction_after_start(tmp_path):
    database_url = f"sqlite:///{tmp_path}/pipeline-late.sqlite3"
    provider = MockProvider()
    service = DailyRacePipeline(provider, database_url)

    result = service.run_once(
        now=datetime(2026, 8, 16, 16, 0, tzinfo=ZoneInfo("Asia/Tokyo")),
        prediction_lead_minutes=120,
    )

    assert result["predictions"][0]["status"] == "missed_prediction_window"
    with make_session_factory(database_url)() as session:
        assert not RaceRepository(session).has_prediction_snapshot("20260816-札幌-11")


def test_daily_pipeline_reviews_backlog_when_schedule_discovery_fails(tmp_path):
    class ScheduleUnavailableProvider(MockProvider):
        def list_races(self, race_date: str):
            raise ProviderUnavailableError("schedule unavailable")

    database_url = f"sqlite:///{tmp_path}/pipeline-backlog.sqlite3"
    provider = ScheduleUnavailableProvider()
    RaceAnalysisService(provider, database_url).analyze(AnalysisRequest("2026-08-16", "札幌", 11))

    result = DailyRacePipeline(provider, database_url).run_once(
        now=datetime(2026, 8, 30, 12, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
    )

    assert result["discovery_error"] == "schedule unavailable"
    assert result["reviews"][0]["status"] == "reviewed"


def test_daily_pipeline_skips_obstacle_races(tmp_path):
    class ObstacleProvider(MockProvider):
        def list_races(self, race_date: str):
            return [RaceScheduleItem(race_date=race_date, racecourse="新潟", race_number=1, start_time="15:00", race_type="obstacle")]

        def get_race(self, race_date: str, racecourse: str, race_number: int):
            raise AssertionError("obstacle race card must not be analyzed")

    result = DailyRacePipeline(
        ObstacleProvider(), f"sqlite:///{tmp_path}/pipeline-obstacle.sqlite3"
    ).run_once(now=datetime(2026, 8, 16, 14, 0, tzinfo=ZoneInfo("Asia/Tokyo")))

    assert result["predictions"][0]["status"] == "unsupported_race_type"
