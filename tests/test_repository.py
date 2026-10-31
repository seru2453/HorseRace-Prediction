from pathlib import Path
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import delete, event, select

from database.models import (
    ChallengerPrediction,
    AnalysisRun,
    Horse,
    ModelEvaluation,
    PastPerformance,
    PredictionMetric,
    PredictionReview,
    PredictionSnapshot,
    Race,
    RaceEntry,
    RaceResultEntry,
    TrainingRun,
)
from database.repository import RaceRepository
from database.session import make_session_factory
from services.app.orchestrator import AnalysisRequest, RaceAnalysisService
from services.data_provider.mock import MockProvider
from services.features.generator import FeatureGenerator
from services.learning.review import PredictionReviewer
from services.parser.race_parser import RaceParser


def test_repository_saves_and_reuses_past_performances(tmp_path):
    parsed = RaceParser().parse_entries_html(Path("fixtures/jra_official_sample.html").read_text(encoding="utf-8"))
    first_entry = parsed.entries[0]
    database_url = f"sqlite:///{tmp_path}/repo.sqlite3"

    with make_session_factory(database_url)() as session:
        session.add(
            Race(
                race_id=parsed.race.race_id,
                race_date=parsed.race.race_date.value,
                racecourse=parsed.race.racecourse.value,
                race_number=parsed.race.race_number.value,
                payload=parsed.race.model_dump(mode="json"),
            )
        )
        session.add(
            Horse(
                horse_id=first_entry.horse.horse_id,
                horse_name=first_entry.horse.horse_name.value,
                payload=first_entry.horse.model_dump(mode="json"),
            )
        )
        session.commit()
        repo = RaceRepository(session)
        repo.save_past_performances(parsed.race.race_id, {first_entry.horse.horse_id: parsed.histories[first_entry.horse.horse_id]})

    with make_session_factory(database_url)() as session:
        rows = session.query(PastPerformance).all()
        history = RaceRepository(session).get_horse_history(first_entry.horse.horse_id)

    assert len(rows) == 2
    assert history[0]["race_date"] == "2026-07-12"
    assert history[0]["finish_position"] == 3
    assert history[0]["distance"] == 2000


def test_batched_horse_histories_limit_rows_and_exclude_future(tmp_path):
    database_url = f"sqlite:///{tmp_path}/batch-history.sqlite3"
    factory = make_session_factory(database_url)
    with factory() as session:
        for horse_id in ("batch-a", "batch-b"):
            session.add(Horse(horse_id=horse_id, horse_name=horse_id, payload={}))
            for day in range(1, 24):
                race_date = f"2026-08-{day:02d}"
                session.add(
                    PastPerformance(
                        horse_id=horse_id,
                        race_date=race_date,
                        racecourse="札幌",
                        race_name=f"race-{horse_id}-{day}",
                        finish_position=day,
                        payload={"race_date": race_date, "finish_position": day},
                    )
                )
        session.commit()

    statements = []
    engine = factory.kw["bind"]
    listener = lambda *args: statements.append(args[2])
    event.listen(engine, "before_cursor_execute", listener)
    try:
        with factory() as session:
            result = RaceRepository(session).get_horse_histories(
                ["batch-a", "batch-b"], limit=20, before_date="2026-08-23"
            )
    finally:
        event.remove(engine, "before_cursor_execute", listener)

    assert len(statements) == 1
    assert len(result["batch-a"]) == len(result["batch-b"]) == 20
    assert result["batch-a"][0]["race_date"] == "2026-08-22"
    assert all(row["race_date"] < "2026-08-23" for rows in result.values() for row in rows)


def test_save_analysis_select_count_does_not_scale_with_entries(tmp_path):
    database_url = f"sqlite:///{tmp_path}/save-analysis-batch.sqlite3"
    factory = make_session_factory(database_url)
    analysis = RaceAnalysisService(provider=MockProvider(), database_url=database_url).analyze(
        AnalysisRequest("2026-08-16", "札幌", 11, iterations=1000, save=False)
    )
    statements = []
    engine = factory.kw["bind"]
    listener = lambda *args: statements.append(args[2])
    event.listen(engine, "before_cursor_execute", listener)
    try:
        with factory() as session:
            RaceRepository(session).save_analysis(analysis)
    finally:
        event.remove(engine, "before_cursor_execute", listener)

    selects = [statement for statement in statements if statement.lstrip().upper().startswith("SELECT")]
    assert len(analysis.entries) > 5
    assert len(selects) == 3  # race, horses, and entries are fetched in batches
    with factory() as session:
        assert session.query(Horse).count() == len(analysis.entries)
        assert session.query(RaceEntry).count() == len(analysis.entries)


def test_save_past_performances_batches_existing_rows_and_updates_without_duplicates(tmp_path):
    database_url = f"sqlite:///{tmp_path}/save-past-batch.sqlite3"
    factory = make_session_factory(database_url)
    histories = {
        horse_id: [
            {"race_date": f"2026-07-{day:02d}", "racecourse": "札幌", "race_name": f"race-{day}", "finish_position": day}
            for day in range(1, 9)
        ]
        for horse_id in ("horse-a", "horse-b")
    }
    with factory() as session:
        RaceRepository(session).save_past_performances("observed", histories)

    histories["horse-a"][0]["finish_position"] = 9
    statements = []
    engine = factory.kw["bind"]
    listener = lambda *args: statements.append(args[2])
    event.listen(engine, "before_cursor_execute", listener)
    try:
        with factory() as session:
            RaceRepository(session).save_past_performances("new-observation", histories)
    finally:
        event.remove(engine, "before_cursor_execute", listener)

    selects = [statement for statement in statements if statement.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 1
    with factory() as session:
        rows = session.scalars(select(PastPerformance)).all()
    assert len(rows) == 16
    updated = next(row for row in rows if row.horse_id == "horse-a" and row.race_date == "2026-07-01")
    assert updated.finish_position == 9
    assert updated.payload["finish_position"] == 9


def test_analysis_reuses_db_history_when_provider_has_no_history(tmp_path):
    class NoHistoryProvider(MockProvider):
        def get_horse_history(self, horse):
            return []

    class CapturingFeatureGenerator(FeatureGenerator):
        def __init__(self):
            self.histories = None

        def generate(self, race, entries, histories=None, **kwargs):
            self.histories = histories
            return super().generate(race, entries, histories, **kwargs)

    database_url = f"sqlite:///{tmp_path}/reuse.sqlite3"
    with make_session_factory(database_url)() as session:
        session.add(Horse(horse_id="demo-h001", horse_name="アオゾラノート", payload={}))
        session.add(
            PastPerformance(
                horse_id="demo-h001",
                observed_in_race_id=None,
                race_date="2026-08-01",
                racecourse="札幌",
                race_name="保存済み過去走",
                finish_position=2,
                payload={"race_date": "2026-08-01", "racecourse": "札幌", "race_name": "保存済み過去走", "finish_position": 2},
            )
        )
        session.commit()

    service = RaceAnalysisService(provider=NoHistoryProvider(), database_url=database_url)
    capture = CapturingFeatureGenerator()
    service.feature_generator = capture
    service.analyze(AnalysisRequest("2026-08-16", "札幌", 11, iterations=1000, save=False))

    assert capture.histories["demo-h001"][0]["race_name"] == "保存済み過去走"


def test_result_enriches_future_history_with_time_gap_without_past_leakage(tmp_path):
    database_url = f"sqlite:///{tmp_path}/result-history.sqlite3"
    provider = MockProvider()
    analysis = RaceAnalysisService(provider=provider, database_url=database_url).analyze(
        AnalysisRequest(
            "2026-08-16",
            "札幌",
            11,
            iterations=1000,
            execution_time=datetime(2026, 8, 16, 5, 0, tzinfo=timezone.utc),
        )
    )
    result = provider.get_race_results(analysis.race.race_id)
    assert result is not None

    with make_session_factory(database_url)() as session:
        repo = RaceRepository(session)
        repo.save_result(result)
        before_race = repo.get_horse_history("demo-h002", before_date="2026-08-16")
        after_race = repo.get_horse_history("demo-h002", before_date="2026-08-17")
        result_entries = session.scalars(
            select(RaceResultEntry).where(RaceResultEntry.race_id == analysis.race.race_id)
        ).all()

    assert before_race == []
    assert after_race[0]["finish_position"] == 2
    assert after_race[0]["time_behind_winner_seconds"] == 0.2
    assert after_race[0]["grade"] == "OP"
    assert after_race[0]["track_condition"] == "良"
    assert len(result_entries) == len(result.entries)
    second = next(row for row in result_entries if row.horse_number == 2)
    assert second.horse_id == "demo-h002"
    assert second.finish_position == 2
    assert second.final_three_furlongs == 35.4
    assert second.field_size == len(analysis.entries)
    assert second.relative_finish == pytest.approx(6 / 7)
    assert second.relative_finish_category == "GOOD"


def test_prediction_snapshots_create_normalized_metrics(tmp_path):
    database_url = f"sqlite:///{tmp_path}/prediction-metrics.sqlite3"
    analysis = RaceAnalysisService(provider=MockProvider(), database_url=database_url).analyze(
        AnalysisRequest("2026-08-16", "札幌", 11, iterations=1000)
    )

    with make_session_factory(database_url)() as session:
        snapshots = session.scalars(select(PredictionSnapshot).order_by(PredictionSnapshot.id)).all()
        metrics = session.scalars(select(PredictionMetric).order_by(PredictionMetric.prediction_snapshot_id)).all()

    assert len(metrics) == len(snapshots) == len(analysis.predictions)
    first_prediction = next(item for item in analysis.predictions if item.horse_id == snapshots[0].horse_id)
    assert metrics[0].prediction_snapshot_id == snapshots[0].id
    assert metrics[0].base_score == first_prediction.base_score
    assert metrics[0].confidence == first_prediction.confidence
    assert metrics[0].rank_delta == first_prediction.rank_delta
    assert metrics[0].current_form == first_prediction.current_form
    assert metrics[0].condition_fit == first_prediction.condition_fit
    assert metrics[0].field_size == len(analysis.entries)


def test_challenger_predictions_are_linked_to_immutable_snapshots(tmp_path):
    database_url = f"sqlite:///{tmp_path}/challenger.sqlite3"
    analysis = RaceAnalysisService(provider=MockProvider(), database_url=database_url).analyze(
        AnalysisRequest("2026-08-16", "札幌", 11, iterations=1000)
    )
    with make_session_factory(database_url)() as session:
        repository = RaceRepository(session)
        snapshot = session.scalar(select(PredictionSnapshot).limit(1))
        assert snapshot is not None
        original_probability = snapshot.ai_win_probability
        repository.save_challenger_predictions(
            {snapshot.horse_id: snapshot.id},
            [
                {
                    "horse_id": snapshot.horse_id,
                    "model_version": "logistic-v1",
                    "feature_version": "feature-v1",
                    "target": "actual_win",
                    "predicted_value": 0.2,
                }
            ],
        )
        repository.save_challenger_predictions(
            {snapshot.horse_id: snapshot.id},
            [
                {
                    "horse_id": snapshot.horse_id,
                    "model_version": "logistic-v1",
                    "feature_version": "feature-v1",
                    "target": "actual_win",
                    "predicted_value": 0.25,
                }
            ],
        )
        rows = session.scalars(select(ChallengerPrediction)).all()
        session.refresh(snapshot)

    assert len(rows) == 1
    assert rows[0].predicted_value == 0.25
    assert snapshot.ai_win_probability == original_probability


def test_training_run_persists_walk_forward_evaluation(tmp_path):
    database_url = f"sqlite:///{tmp_path}/training-run.sqlite3"
    dataset = {
        "row_count": 24,
        "race_count": 4,
        "rows": [
            {"race_date": "2026-08-01", "feature_version": "feature-v1"},
            {"race_date": "2026-08-16", "feature_version": "feature-v1"},
        ],
    }
    metrics = {
        "status": "trained_in_sample",
        "label": "actual_win",
        "row_count": 24,
        "race_count": 4,
        "race_holdout": {
            "status": "evaluated",
            "fold_count": 3,
            "sample_count": 18,
            "blended_brier_score": 0.12,
            "blended_log_loss": 0.41,
            "blended_calibration_error": 0.06,
            "blended_top3_hit_rate": 0.67,
        },
    }

    with make_session_factory(database_url)() as session:
        repository = RaceRepository(session)
        repository.save_training_run("model-v1", "feature-v1", dataset, metrics)
        run = session.scalar(select(TrainingRun))
        evaluation = session.scalar(select(ModelEvaluation))
        exported = repository.export_data()

    assert run.training_start == "2026-08-01"
    assert run.training_end == "2026-08-16"
    assert run.row_count == 24
    assert evaluation.evaluation_period == "2026-08-01..2026-08-16"
    assert evaluation.calibration_error == 0.06
    assert evaluation.top3_hit_rate == 0.67
    assert exported["counts"]["training_runs"] == 1
    assert exported["counts"]["model_evaluations"] == 1

    imported_url = f"sqlite:///{tmp_path}/training-run-import.sqlite3"
    with make_session_factory(imported_url)() as session:
        stats = RaceRepository(session).import_data(exported)
        assert stats["imported"]["training_runs"] == 1
        assert stats["imported"]["model_evaluations"] == 1
        assert session.scalar(select(TrainingRun)).metrics_json["label"] == "actual_win"
        assert session.scalar(select(ModelEvaluation)).brier_score == 0.12


def test_session_backfills_normalized_rows_from_legacy_json(tmp_path):
    database_url = f"sqlite:///{tmp_path}/normalized-backfill.sqlite3"
    provider = MockProvider()
    analysis = RaceAnalysisService(provider=provider, database_url=database_url).analyze(
        AnalysisRequest("2026-08-16", "札幌", 11, iterations=1000)
    )
    result = provider.get_race_results(analysis.race.race_id)
    assert result is not None

    with make_session_factory(database_url)() as session:
        RaceRepository(session).save_result(result)
        session.execute(delete(PredictionMetric))
        session.execute(delete(RaceResultEntry))
        session.commit()

    with make_session_factory(database_url)() as session:
        metrics = session.scalars(select(PredictionMetric)).all()
        result_entries = session.scalars(select(RaceResultEntry)).all()

    assert len(metrics) == len(analysis.predictions)
    assert len(result_entries) == len(result.entries)


def test_review_save_is_idempotent_and_duplicate_reads_use_latest_review(tmp_path):
    database_url = f"sqlite:///{tmp_path}/review-dedup.sqlite3"
    provider = MockProvider()
    analysis = RaceAnalysisService(provider=provider, database_url=database_url).analyze(
        AnalysisRequest(
            "2026-08-16",
            "札幌",
            11,
            iterations=1000,
            execution_time=datetime(2026, 8, 16, 5, 0, tzinfo=timezone.utc),
        )
    )
    result = provider.get_race_results(analysis.race.race_id)
    review = PredictionReviewer().review(analysis.predictions, result)

    with make_session_factory(database_url)() as session:
        repo = RaceRepository(session)
        repo.save_result(result)
        repo.save_review(analysis.race.race_id, review)
        repo.save_review(analysis.race.race_id, {**review, "note": "latest"})
        stored = session.scalars(
            select(PredictionReview).where(PredictionReview.race_id == analysis.race.race_id)
        ).all()
        assert len(stored) == 1

        session.add(
            PredictionReview(
                race_id=analysis.race.race_id,
                payload={**review, "note": "legacy duplicate"},
                created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
            )
        )
        first_prediction = analysis.predictions[0]
        session.add(
            PredictionSnapshot(
                race_id=analysis.race.race_id,
                horse_id=first_prediction.horse_id,
                horse_number=first_prediction.horse_number,
                prediction_timestamp=datetime(2030, 1, 1, tzinfo=timezone.utc),
                model_version="post-result-model",
                feature_version="post-result-feature",
                simulation_seed=1,
                ai_win_probability=0.99,
                ai_top2_probability=0.99,
                ai_top3_probability=0.99,
                raw_features={"post_result_only": True},
                prediction_payload=first_prediction.model_dump(mode="json"),
            )
        )
        session.commit()

        dataset = repo.training_dataset()
        summary = repo.learning_summary()
        history = repo.race_history()

    assert dataset["row_count"] == len(analysis.predictions)
    assert len({(row["race_id"], row["horse_number"]) for row in dataset["rows"]}) == dataset["row_count"]
    assert "actual_top5" in dataset["label_columns"]
    assert all(
        row["actual_top5"] == (int(row["finish_position"]) <= 5 if row["finish_position"] is not None else None)
        for row in dataset["rows"]
    )
    assert "actual_top5" not in dataset["feature_columns"]
    assert all(row["model_version"] != "post-result-model" for row in dataset["rows"])
    assert summary["review_count"] == 1
    assert summary["ranking_metric_summary"]["top5_race_count"] == 0
    assert summary["ranking_metric_summary"]["top5_sample_status"] == "sample_small"
    assert history[0]["review_count"] == 1


def test_top5_ranking_summary_aggregates_rates_and_marks_small_samples():
    summary = RaceRepository._ranking_metric_summary(
        object(),
        [
            {"ranking_metrics": {
                "winner_in_top5": True,
                "top5_capture_count": 5,
                "top5_capture_rate": 1.0,
                "top5_capture_4of5": True,
                "top5_capture_5of5": True,
            }},
            {"ranking_metrics": {
                "winner_in_top5": False,
                "top5_capture_count": 3,
                "top5_capture_rate": 0.6,
                "top5_capture_4of5": False,
                "top5_capture_5of5": False,
            }},
        ],
    )

    assert summary["top5_race_count"] == 2
    assert summary["winner_in_top5_rate"] == 0.5
    assert summary["average_top5_capture_count"] == 4
    assert summary["average_top5_capture_rate"] == 0.8
    assert summary["top5_capture_4of5_rate"] == 0.5
    assert summary["top5_capture_5of5_rate"] == 0.5
    assert summary["top5_sample_status"] == "sample_small"


def test_training_dataset_excludes_obstacle_race(tmp_path):
    database_url = f"sqlite:///{tmp_path}/obstacle-training.sqlite3"
    provider = MockProvider()
    analysis = RaceAnalysisService(provider=provider, database_url=database_url).analyze(
        AnalysisRequest(
            "2026-08-16",
            "札幌",
            11,
            execution_time=datetime(2026, 8, 16, 5, 0, tzinfo=timezone.utc),
        )
    )
    result = provider.get_race_results(analysis.race.race_id)
    review = PredictionReviewer().review(analysis.predictions, result)

    with make_session_factory(database_url)() as session:
        repo = RaceRepository(session)
        repo.save_result(result)
        repo.save_review(analysis.race.race_id, review)
        race = session.get(Race, analysis.race.race_id)
        race.payload = {**race.payload, "surface": {"value": "障害"}, "race_class": {"value": "障害3歳以上"}}
        session.commit()
        dataset = repo.training_dataset()

    assert dataset["row_count"] == 0
    assert dataset["excluded_counts"]["unsupported_race_type"] == 1


def test_learning_trend_summary_uses_recent_reviews(tmp_path):
    database_url = f"sqlite:///{tmp_path}/trend.sqlite3"
    base_time = datetime(2026, 8, 16, tzinfo=timezone.utc)
    rows = [
        (0.30, 0.10),
        (0.25, 0.12),
        (0.18, 0.20),
        (0.14, 0.24),
    ]
    with make_session_factory(database_url)() as session:
        for index, (ai_brier, user_brier) in enumerate(rows):
            session.add(
                PredictionReview(
                    race_id=f"race-{index}",
                    payload={
                        "ai_brier_score": ai_brier,
                        "user_brier_score": user_brier,
                        "comparison": {"winner": "ai", "brier_delta": user_brier - ai_brier},
                        "rows": [],
                    },
                    created_at=base_time + timedelta(days=index),
                )
            )
        session.commit()
        summary = RaceRepository(session).learning_summary()

    assert summary["trend_summary"]["recent_count"] == 4
    assert summary["trend_summary"]["ai_recent_brier_score"] == 0.2175
    assert summary["trend_summary"]["user_recent_brier_score"] == 0.165
    assert summary["trend_summary"]["ai_direction"] == "improving"
    assert summary["trend_summary"]["user_direction"] == "worsening"


def test_learning_summary_suggests_factor_weight_adjustments(tmp_path):
    database_url = f"sqlite:///{tmp_path}/factor.sqlite3"
    with make_session_factory(database_url)() as session:
        session.add(
            PredictionReview(
                race_id="race-factor",
                payload={
                    "ai_brier_score": 0.22,
                    "factor_attribution": {
                        "overvalued_positive_factors": [
                            {
                                "factor_key": "市場人気上位",
                                "factor": "市場人気上位 +0.12",
                                "count": 3,
                                "average_amount": 0.12,
                                "average_ai_probability": 0.18,
                                "average_ai_brier": 0.31,
                                "samples": [],
                                "statistical_note": "sample_small",
                            }
                        ],
                        "winner_positive_factors": [
                            {
                                "factor_key": "近走複勝率",
                                "factor": "近走複勝率 +0.20",
                                "count": 1,
                                "average_amount": 0.20,
                                "average_ai_probability": 0.16,
                                "average_ai_brier": 0.70,
                                "samples": [],
                                "statistical_note": "sample_small",
                            }
                        ],
                        "winner_negative_factors": [
                            {
                                "factor_key": "近走振れ幅大",
                                "factor": "近走振れ幅大 -0.04",
                                "count": 1,
                                "average_amount": -0.04,
                                "average_ai_probability": 0.16,
                                "average_ai_brier": 0.70,
                                "samples": [],
                                "statistical_note": "sample_small",
                            }
                        ],
                    },
                    "rows": [],
                },
            )
        )
        session.commit()
        summary = RaceRepository(session).learning_summary()

    recommendations = {row["factor"]: row for row in summary["factor_weight_recommendations"]}
    recommendation_keys = {row["factor_key"]: row for row in summary["factor_weight_recommendations"]}

    assert recommendations["市場人気上位 +0.12"]["action"] == "decrease_positive_weight"
    assert recommendations["市場人気上位 +0.12"]["suggested_delta"] == -0.03
    assert recommendations["近走振れ幅大 -0.04"]["action"] == "weaken_negative_penalty"
    assert recommendations["近走振れ幅大 -0.04"]["suggested_delta"] == 0.01
    assert recommendations["近走振れ幅大 -0.04"]["readiness"] == "monitor"
    assert recommendations["近走振れ幅大 -0.04"]["sample_count_status"] == "low"
    assert recommendation_keys["市場人気上位"]["factor"] == "市場人気上位 +0.12"
    assert any(item["type"] == "factor_tuning" for item in summary["learning_insights"])


def test_learning_summary_prioritizes_rank_miss_factor_evidence(tmp_path):
    database_url = f"sqlite:///{tmp_path}/rank-factor.sqlite3"
    with make_session_factory(database_url)() as session:
        session.add(
            PredictionReview(
                race_id="race-rank-factor",
                payload={
                    "factor_attribution": {
                        "rank_overvalued_positive_factors": [
                            {
                                "factor_key": "市場支持",
                                "factor": "市場支持 +0.10",
                                "count": 1,
                                "average_amount": 0.10,
                                "average_ai_probability": 0.10,
                                "average_ai_brier": 0.01,
                                "average_absolute_rank_error": 5.0,
                                "samples": [],
                                "statistical_note": "sample_small",
                            }
                        ]
                    },
                    "rows": [],
                },
            )
        )
        session.commit()
        summary = RaceRepository(session).learning_summary()

    factor = summary["factor_attribution_summary"]["rank_overvalued_positive_factors"][0]
    recommendation = next(
        row for row in summary["factor_weight_recommendations"] if row["factor_key"] == "市場支持"
    )
    assert factor["average_absolute_rank_error"] == 5.0
    assert recommendation["evidence"] == "rank_overvaluation"
    assert recommendation["average_absolute_rank_error"] == 5.0


def test_factor_weight_recommendation_readiness_uses_sample_and_gap(tmp_path):
    database_url = f"sqlite:///{tmp_path}/factor-readiness.sqlite3"
    with make_session_factory(database_url)() as session:
        session.add(
            PredictionReview(
                race_id="race-factor-ready",
                payload={
                    "factor_attribution": {
                        "overvalued_positive_factors": [
                            {
                                "factor_key": "市場人気上位",
                                "factor": "市場人気上位 +0.12",
                                "count": 30,
                                "average_amount": 0.12,
                                "average_ai_probability": 0.20,
                                "average_ai_brier": 0.18,
                                "samples": [],
                                "statistical_note": "ok",
                            }
                        ],
                    },
                    "rows": [
                        {
                            "actual_win": 0.0,
                            "ai_probability": 0.12,
                            "ai_brier": 0.0144,
                            "factor_impacts": [
                                {
                                    "factor_key": "市場人気上位",
                                    "factor": "市場人気上位 +0.12",
                                    "direction": "positive",
                                    "amount": 0.12,
                                }
                            ],
                        }
                        for _ in range(30)
                    ],
                },
            )
        )
        session.commit()
        repo = RaceRepository(session)
        summary = repo.learning_summary()
        active_adjustments = repo.active_factor_adjustments()

    recommendation = summary["factor_weight_recommendations"][0]

    assert recommendation["factor_key"] == "市場人気上位"
    assert recommendation["readiness"] == "candidate"
    assert recommendation["sample_count_status"] == "enough"
    assert recommendation["gap_status"] == "large"
    assert recommendation["calibration_gap"] == 0.12
    assert recommendation["deployment_status"] == "challenger"
    assert active_adjustments == {}


def test_learning_summary_groups_factor_amount_variants(tmp_path):
    database_url = f"sqlite:///{tmp_path}/factor-group.sqlite3"
    with make_session_factory(database_url)() as session:
        session.add(
            PredictionReview(
                race_id="race-factor-group",
                payload={
                    "factor_attribution": {
                        "overvalued_positive_factors": [
                            {
                                "factor_key": "近走複勝率",
                                "factor": "近走複勝率 +0.18",
                                "count": 1,
                                "average_amount": 0.18,
                                "average_ai_probability": 0.12,
                                "average_ai_brier": 0.20,
                                "samples": [],
                            },
                            {
                                "factor_key": "近走複勝率",
                                "factor": "近走複勝率 +0.24",
                                "count": 2,
                                "average_amount": 0.24,
                                "average_ai_probability": 0.15,
                                "average_ai_brier": 0.30,
                                "samples": [],
                            },
                        ],
                    },
                    "rows": [],
                },
            )
        )
        session.commit()
        summary = RaceRepository(session).learning_summary()

    rows = summary["factor_attribution_summary"]["overvalued_positive_factors"]
    grouped = {row["factor_key"]: row for row in rows}

    assert grouped["近走複勝率"]["count"] == 3
    assert grouped["近走複勝率"]["average_amount"] == 0.22
    assert grouped["近走複勝率"]["average_ai_brier"] == 0.266667


def test_learning_summary_calibrates_factor_keys(tmp_path):
    database_url = f"sqlite:///{tmp_path}/factor-calibration.sqlite3"
    with make_session_factory(database_url)() as session:
        session.add(
            PredictionReview(
                race_id="race-factor-calibration",
                payload={
                    "rows": [
                        {
                            "horse_number": 1,
                            "actual_win": 0.0,
                            "ai_probability": 0.20,
                            "ai_brier": 0.04,
                            "factor_impacts": [
                                {
                                    "factor_key": "近走複勝率",
                                    "factor": "近走複勝率 +0.18",
                                    "direction": "positive",
                                    "amount": 0.18,
                                }
                            ],
                        },
                        {
                            "horse_number": 2,
                            "actual_win": 1.0,
                            "ai_probability": 0.10,
                            "ai_brier": 0.81,
                            "factor_impacts": [
                                {
                                    "factor_key": "近走複勝率",
                                    "factor": "近走複勝率 +0.24",
                                    "direction": "positive",
                                    "amount": 0.24,
                                },
                                {
                                    "factor_key": "近走振れ幅大",
                                    "factor": "近走振れ幅大 -0.04",
                                    "direction": "negative",
                                    "amount": -0.04,
                                },
                            ],
                        },
                    ],
                },
            )
        )
        session.commit()
        summary = RaceRepository(session).learning_summary()

    positive = {row["factor_key"]: row for row in summary["factor_calibration_summary"]["positive"]}
    negative = {row["factor_key"]: row for row in summary["factor_calibration_summary"]["negative"]}

    assert positive["近走複勝率"]["count"] == 2
    assert positive["近走複勝率"]["average_ai_probability"] == 0.15
    assert positive["近走複勝率"]["actual_win_rate"] == 0.5
    assert positive["近走複勝率"]["calibration_gap"] == -0.35
    assert negative["近走振れ幅大"]["calibration_gap"] == -0.9


def test_learning_data_quality_attention_runs_are_ranked(tmp_path):
    database_url = f"sqlite:///{tmp_path}/quality.sqlite3"
    with make_session_factory(database_url)() as session:
        session.add(Race(race_id="race-good", race_date="2026-08-16", racecourse="札幌", race_number=1, payload={}))
        session.add(Race(race_id="race-bad", race_date="2026-08-16", racecourse="札幌", race_number=2, payload={}))
        session.add(
            AnalysisRun(
                race_id="race-good",
                provider_name="MockProvider",
                parse_status="ok",
                recognized_entries=8,
                expected_entries=8,
                issue_count=0,
                payload={"feature_quality": {"available_feature_ratio": 0.9, "missing_history_count": 0, "unavailable_counts": {"jockey_win_rate": 2}}},
            )
        )
        session.add(
            AnalysisRun(
                race_id="race-bad",
                provider_name="MockProvider",
                parse_status="warning",
                recognized_entries=6,
                expected_entries=8,
                issue_count=3,
                payload={"feature_quality": {"available_feature_ratio": 0.4, "missing_history_count": 5, "unavailable_counts": {"jockey_win_rate": 6, "past_5_average_finish": 5}}},
            )
        )
        session.commit()
        summary = RaceRepository(session).learning_summary()

    assert summary["data_quality_summary"]["analysis_run_count"] == 2
    assert summary["data_quality_summary"]["top_unavailable_features"][0] == {"feature": "jockey_win_rate", "count": 8}
    assert summary["data_quality_summary"]["attention_runs"][0]["race_id"] == "race-bad"
    assert summary["data_quality_summary"]["attention_runs"][0]["issue_count"] == 3
