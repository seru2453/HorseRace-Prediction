from database.repository import RaceRepository
from database.session import make_session_factory
from database.models import PredictionReview, PredictionSnapshot
from sqlalchemy import select
from services.app.orchestrator import AnalysisRequest, RaceAnalysisService
from services.common.models import UserPredictionInput
from services.data_provider.mock import MockProvider
from services.learning.review import PredictionReviewer


def test_demo_flow_analyze_save_result_and_review(tmp_path):
    database_url = f"sqlite:///{tmp_path}/flow.sqlite3"
    provider = MockProvider()
    service = RaceAnalysisService(provider=provider, database_url=database_url)

    analysis = service.analyze(AnalysisRequest("2026-08-16", "札幌", 11, iterations=1000, seed=123))

    assert analysis.race.race_id == "20260816-sapporo-11"
    assert analysis.parse_quality.status == "ok"
    assert len(analysis.entries) == 8
    assert len(analysis.predictions) == 8
    assert analysis.simulation.iterations == 1000

    with make_session_factory(database_url)() as session:
        snapshots = session.scalars(
            select(PredictionSnapshot).where(PredictionSnapshot.race_id == analysis.race.race_id)
        ).all()
        snapshot_payloads = RaceRepository(session).prediction_snapshots(analysis.race.race_id, limit=len(analysis.predictions))

    assert len(snapshots) == len(analysis.predictions)
    assert len(snapshot_payloads) == len(analysis.predictions)
    assert snapshots[0].simulation_seed == 123
    assert snapshots[0].model_version == analysis.predictions[0].model_version
    assert snapshots[0].feature_version == analysis.predictions[0].feature_version
    assert snapshots[0].raw_features["surface"] == "芝"
    assert "past_5_average_finish" in snapshots[0].raw_features
    assert snapshots[0].prediction_payload["win_probability"] == snapshots[0].ai_win_probability
    assert snapshot_payloads[0]["prediction"]["horse_name"]

    user_predictions = {
        5: UserPredictionInput(
            mark="◎",
            user_win_probability=0.3,
            confidence=4,
            track_rating=2,
            pedigree_rating=1,
            jockey_rating=0,
            rotation_rating=-1,
            pace_rating=2,
            comment="デモ予想",
        )
    }
    result = provider.get_race_results(analysis.race.race_id)
    assert result is not None

    with make_session_factory(database_url)() as session:
        repo = RaceRepository(session)
        repo.save_user_predictions(analysis.race.race_id, user_predictions)
        repo.save_result(result)

    review = PredictionReviewer().review(analysis.predictions, result, user_predictions)
    assert review["ai_brier_score"] is not None
    assert review["user_brier_score"] is not None
    assert review["comparison"]["winner"] in {"ai", "user", "tie"}
    assert review["user_probability_summary"]["probability_total"] == 0.3
    assert review["user_probability_summary"]["filled_count"] == 1
    assert review["user_probability_summary"]["horse_count"] == len(analysis.predictions)
    assert review["user_probability_summary"]["status"] == "under_total"
    assert review["insights"]["biggest_ai_misses"]
    assert review["factor_attribution"]["overvalued_positive_factors"]
    assert "score_factors" in review["rows"][0]
    assert "factor_impacts" in review["rows"][0]
    assert "factor_key" in review["rows"][0]["factor_impacts"][0]
    assert any(row["user_ratings"].get("track_rating") == 2 for row in review["rows"])
    assert review["rows"][0]["finish_position"] is not None or review["rows"][0]["result_status"] is not None
    assert review["rows"][0]["base_score"] is not None
    assert "condition_adjustment" in review["rows"][0]
    assert "rank_delta" in review["rows"][0]
    assert "pedigree_adjustment" in review["rows"][0]
    assert "ability_index" in review["rows"][0]
    assert "ability_components" in review["rows"][0]
    assert review["ranking_metrics"]["winner_predicted_rank"] is not None

    with make_session_factory(database_url)() as session:
        repo = RaceRepository(session)
        repo.save_review(analysis.race.race_id, review)
        stored = session.scalars(select(PredictionReview).where(PredictionReview.race_id == analysis.race.race_id)).all()

    assert len(stored) == 1
    assert stored[0].payload["ai_brier_score"] is not None

    with make_session_factory(database_url)() as session:
        summary = RaceRepository(session).learning_summary()
        history = RaceRepository(session).race_history()
        detail = RaceRepository(session).race_detail(analysis.race.race_id)

    assert summary["review_count"] == 1
    assert history[0]["parse_status"] == "ok"
    assert history[0]["available_feature_ratio"] is not None
    assert detail["latest_analysis_run"]["parse_status"] == "ok"
    assert detail["latest_analysis_run"]["payload"]["feature_quality"]["horse_count"] == len(analysis.entries)
    assert summary["ai_brier_score"] is not None
    assert summary["comparison_summary"]["ai_wins"] + summary["comparison_summary"]["user_wins"] + summary["comparison_summary"]["ties"] == 1
    assert summary["comparison_summary"]["average_brier_delta"] is not None
    assert summary["trend_summary"]["recent_count"] == 1
    assert summary["trend_summary"]["ai_direction"] == "insufficient"
    assert summary["user_probability_quality_summary"]["review_count"] == 1
    assert summary["user_probability_quality_summary"]["average_probability_total"] == 0.3
    assert summary["user_probability_quality_summary"]["average_coverage"] == 0.125
    assert summary["user_probability_quality_summary"]["status_counts"]["under_total"] == 1
    assert summary["data_quality_summary"]["analysis_run_count"] == 1
    assert summary["data_quality_summary"]["parse_status_counts"]["ok"] == 1
    assert summary["data_quality_summary"]["average_available_feature_ratio"] is not None
    assert summary["calibration_summary"]["ai"]
    assert summary["calibration_summary"]["user"]
    assert summary["factor_attribution_summary"]["overvalued_positive_factors"]
    assert summary["factor_calibration_summary"]["positive"]
    assert summary["learning_insights"]
    assert summary["ranking_metric_summary"]["race_count"] == 1
    breakdown = {(item["dimension"], item["value"]): item for item in summary["condition_breakdown"]}
    assert ("racecourse", "札幌") in breakdown
    assert ("surface", "芝") in breakdown
    assert ("distance_band", "middle") in breakdown
    assert ("track_condition", "良") in breakdown
    assert breakdown[("surface", "芝")]["count"] == 1
    horse_breakdown = {(item["dimension"], item["value"]): item for item in summary["horse_breakdown"]}
    assert ("frame_number", "5") in horse_breakdown
    assert ("sex", "牝") in horse_breakdown
    assert ("age_band", "age_4_5") in horse_breakdown
    assert ("popularity_band", "favored_1_3") in horse_breakdown
    rating_breakdown = {(item["dimension"], item["value"]): item for item in summary["user_rating_breakdown"]}
    assert ("track_rating", "2") in rating_breakdown
    assert ("pace_rating", "2") in rating_breakdown
