from argparse import Namespace
from datetime import date

from apps.cli.predict import (
    _strength_stars,
    format_baseline_training_result,
    format_batch_review,
    format_prediction_report,
    format_review_report,
    format_review_unavailable,
    format_saved_races,
    format_training_dataset_summary,
    parse_args,
    review_saved,
    write_training_dataset_csv,
)
from services.app.orchestrator import AnalysisRequest, RaceAnalysisService
from services.common.models import UserPredictionInput
from services.data_provider.mock import MockProvider
from services.learning.review import PredictionReviewer


def test_cli_defaults_to_live_jra_and_today():
    args = parse_args([])

    assert args.mode == "jra"
    assert args.date == date.today().isoformat()


def test_prediction_report_contains_practical_sections(tmp_path):
    service = RaceAnalysisService(provider=MockProvider(), database_url=f"sqlite:///{tmp_path}/cli.sqlite3")
    result = service.analyze(AnalysisRequest("2026-08-16", "札幌", 11, iterations=1000, save=False))

    report = format_prediction_report(result)

    assert "# 2026-08-16 札幌 11R デモサマーステークス" in report
    assert "## AI予想 上位" in report
    assert "市場:" in report
    assert "## Simulation レース判定" in report
    assert "## 候補セット" in report
    assert "## 展開一致度" in report
    assert "購入連携や利益保証ではなく" in report


def test_strength_stars_use_half_steps():
    assert _strength_stars(0.10, 0.10) == "★★★★★ 5.0"
    assert _strength_stars(0.085, 0.10) == "★★★★½ 4.5"
    assert _strength_stars(0.075, 0.10) == "★★★★☆ 4.0"
    assert _strength_stars(0.065, 0.10) == "★★★½☆ 3.5"


def test_training_dataset_summary_and_csv(tmp_path):
    dataset = {
        "row_count": 1,
        "race_count": 1,
        "feature_columns": ["surface"],
        "label_columns": ["actual_win", "actual_top3", "finish_position"],
        "note": "note",
        "rows": [
            {
                "race_id": "race",
                "race_date": "2026-08-16",
                "racecourse": "札幌",
                "race_number": 11,
                "horse_id": "horse",
                "horse_number": 5,
                "horse_name": "ナツノカゼ",
                "snapshot_id": 1,
                "prediction_timestamp": "2026-08-16T00:00:00+00:00",
                "review_created_at": "2026-08-16T01:00:00+00:00",
                "model_version": "rule-based-mvp-v29",
                "feature_version": "mvp-feature-v28",
                "ai_win_probability": 0.3,
                "ai_top2_probability": 0.5,
                "ai_top3_probability": 0.7,
                "base_score": 2.0,
                "confidence": 0.5,
                "finish_position": 1,
                "actual_win": 1.0,
                "actual_top3": True,
                "result_status": None,
                "features": {"surface": "芝"},
            }
        ],
    }
    csv_path = tmp_path / "training.csv"

    summary = format_training_dataset_summary(dataset)
    write_training_dataset_csv(dataset, str(csv_path))

    assert "rows 1 / races 1 / features 1" in summary
    assert "feature__surface" in csv_path.read_text(encoding="utf-8")


def test_baseline_training_result_summary():
    result = {
        "status": "trained_in_sample",
        "model_type": "logistic_regression",
        "label": "actual_win",
        "row_count": 24,
        "race_count": 3,
        "positive_count": 4,
        "negative_count": 20,
        "feature_count": 12,
        "brier_score": 0.12,
        "log_loss": 0.4,
        "top_positive_coefficients": [{"feature": "ai_win_probability", "coefficient": 1.2}],
        "top_negative_coefficients": [{"feature": "feature__surface_ダート", "coefficient": -0.5}],
        "race_holdout": {
            "status": "evaluated",
            "rule_brier_score": 0.2,
            "blended_brier_score": 0.18,
            "blend_approved": True,
            "fold_count": 3,
            "rule_top1_accuracy": 0.2,
            "blended_top1_accuracy": 0.3,
            "rule_top3_hit_rate": 0.5,
            "blended_top3_hit_rate": 0.6,
        },
        "note": "note",
    }

    summary = format_baseline_training_result(result)

    assert "# Challenger学習" in summary
    assert "Brier 0.12 / LogLoss 0.4" in summary
    assert "Race holdout rule Brier 0.2 / blended Brier 0.18 / blend approved / folds 3" in summary
    assert "top1 0.2 -> 0.3 / winner-in-top3 0.5 -> 0.6" in summary
    assert "ai_win_probability" in summary


def test_review_report_contains_result_check(tmp_path):
    provider = MockProvider()
    service = RaceAnalysisService(provider=provider, database_url=f"sqlite:///{tmp_path}/cli.sqlite3")
    result = service.analyze(AnalysisRequest("2026-08-16", "札幌", 11, iterations=1000, save=False))
    race_result = provider.get_race_results(result.race.race_id)
    review = PredictionReviewer().review(result.predictions, race_result)

    report = format_review_report(result, review)

    assert "## 結果レビュー" in report
    assert "AI Brier" in report
    assert "勝ち馬:" in report
    assert "AI上位3頭の3着内ヒット:" in report


def test_mark_only_user_prediction_is_converted_for_review(tmp_path):
    provider = MockProvider()
    service = RaceAnalysisService(provider=provider, database_url=f"sqlite:///{tmp_path}/cli.sqlite3")
    result = service.analyze(AnalysisRequest("2026-08-16", "札幌", 11, iterations=1000, save=False))
    race_result = provider.get_race_results(result.race.race_id)

    review = PredictionReviewer().review(
        result.predictions,
        race_result,
        {5: UserPredictionInput(mark="◎")},
    )

    row = next(item for item in review["rows"] if item["horse_number"] == 5)
    assert row["user_probability"] == 0.30
    assert row["user_brier"] is not None
    assert review["user_brier_score"] is not None
    assert review["user_probability_summary"]["filled_count"] == 1


def test_review_unavailable_report_is_nonfatal_message():
    report = format_review_unavailable("結果未掲載")

    assert "## 結果レビュー" in report
    assert "結果レビューは実行できませんでした: 結果未掲載" in report


def test_saved_races_report_lists_review_state():
    report = format_saved_races(
        [
            {
                "race_id": "20260816-札幌-11",
                "race_date": "2026-08-16",
                "racecourse": "札幌",
                "race_number": 11,
                "race_name": "札幌記念",
                "snapshot_count": 16,
                "has_result": False,
                "review_count": 0,
            }
        ]
    )

    assert "# 保存済み予想" in report
    assert "20260816-札幌-11" in report
    assert "レビュー 0件" in report


def test_batch_review_report_lists_reviewed_and_skipped():
    report = format_batch_review(
        [
            {"race_id": "race-ok", "status": "reviewed", "ai_brier_score": 0.12, "top3_hit_count": 2},
            {"race_id": "race-skip", "status": "skipped", "message": "結果未取得"},
        ]
    )

    assert "race-ok / レビュー保存" in report
    assert "race-skip / skip / 結果未取得" in report


def test_review_saved_uses_latest_snapshots(tmp_path, monkeypatch, capsys):
    database_url = f"sqlite:///{tmp_path}/cli.sqlite3"
    monkeypatch.setenv("DATABASE_URL", database_url)
    provider = MockProvider()
    service = RaceAnalysisService(provider=provider, database_url=database_url)
    result = service.analyze(AnalysisRequest("2026-08-16", "札幌", 11, iterations=1000, save=True))

    exit_code = review_saved(
        Namespace(review_saved_race_id=result.race.race_id, json=False),
        provider,
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "保存済みAI予測レビュー" in output
    assert "## 結果レビュー" in output
