from database.repository import RaceRepository
from database.session import make_session_factory
from services.app.orchestrator import AnalysisRequest, RaceAnalysisService
from services.data_provider.mock import MockProvider
from services.learning.auto_review import AutomaticReviewService


def test_automatic_review_uses_saved_prediction_and_is_idempotent(tmp_path):
    database_url = f"sqlite:///{tmp_path}/auto-review.sqlite3"
    provider = MockProvider()
    RaceAnalysisService(provider=provider, database_url=database_url).analyze(
        AnalysisRequest("2026-08-16", "札幌", 11, iterations=1000)
    )

    service = AutomaticReviewService(provider, database_url)
    first = service.run_once()
    second = service.run_once()

    assert first[0]["status"] == "reviewed"
    assert second == []
    with make_session_factory(database_url)() as session:
        history = RaceRepository(session).race_history(has_review=True)
    assert history[0]["review_count"] == 1


def test_automatic_review_retries_a_prior_race_with_review_but_no_saved_result(tmp_path):
    database_url = f"sqlite:///{tmp_path}/auto-review-missing-result.sqlite3"
    provider = MockProvider()
    analysis = RaceAnalysisService(provider=provider, database_url=database_url).analyze(
        AnalysisRequest("2026-08-16", "札幌", 11, iterations=1000)
    )
    with make_session_factory(database_url)() as session:
        RaceRepository(session).save_review(analysis.race.race_id, {"ai_brier_score": None})

    outcomes = AutomaticReviewService(provider, database_url).run_once()

    assert outcomes[0]["race_id"] == analysis.race.race_id
    assert outcomes[0]["status"] == "result_saved"
    assert outcomes[0]["result_saved"] is True
    assert outcomes[0]["review_saved"] is False
    with make_session_factory(database_url)() as session:
        result, _ = RaceRepository(session).saved_result(analysis.race.race_id)
        history = RaceRepository(session).race_history(has_review=True)
    assert result is not None
    assert history[0]["review_count"] == 1
