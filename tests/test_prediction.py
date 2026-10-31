from datetime import datetime, timezone
import logging
import math
from statistics import pstdev

import pytest

from database.models import PredictionReview
from database.repository import RaceRepository
from database.session import make_session_factory
from services.common.models import HorseFeature, PredictionData, RaceResultData, RaceResultEntryData
from services.app.orchestrator import AnalysisRequest, RaceAnalysisService
from services.data_provider.mock import MockProvider
from services.learning.review import PredictionReviewer
from services.ml.artifacts import save_logistic_artifact
from services.ml.baseline import LogisticBaselineTrainer, TrainingConfig
from services.prediction.config import StabilityConfig
from services.prediction.rule_based import RuleBasedPredictor, _factor_impact, _soft_saturate
from services.prediction.stability_evaluation import evaluate_score_stability


def test_prediction_probabilities_are_bounded_and_normalized(tmp_path):
    service = RaceAnalysisService(provider=MockProvider(), database_url=f"sqlite:///{tmp_path}/test.sqlite3")
    result = service.analyze(AnalysisRequest("2026-08-16", "札幌", 11, iterations=1000, save=False))

    total_win = sum(item.win_probability for item in result.predictions)
    total_raw = sum(item.raw_probability for item in result.predictions)
    total_calibrated = sum(item.calibrated_probability for item in result.predictions)
    raw_stddev = pstdev([item.raw_probability for item in result.predictions])
    calibrated_stddev = pstdev([item.calibrated_probability for item in result.predictions])
    assert 0.999 <= total_win <= 1.001
    assert 0.999 <= total_raw <= 1.001
    assert 0.999 <= total_calibrated <= 1.001
    for item in result.predictions:
        assert 0 <= item.win_probability <= 1
        assert 0 <= item.top2_probability <= 1
        assert 0 <= item.top3_probability <= 1
        assert item.explanation["negative"]
        assert item.explanation["score_factors"][0] == "基礎点 +1.00"
        assert item.model_version == "baseline-v1"
        assert item.feature_version == "mvp-feature-v33"
        diagnostics = item.probability_diagnostics
        assert diagnostics["relative_score_index"] is not None
        assert 0 <= diagnostics["normalized_entropy"] <= 1
        assert diagnostics["raw_probability_stddev"] >= 0
        assert diagnostics["calibrated_probability_stddev"] >= 0
        assert diagnostics["calibration_spread_ratio"] is None or diagnostics["calibration_spread_ratio"] <= 1.001
        assert diagnostics["leader_probability"] >= item.win_probability
        assert diagnostics["leader_gap_to_second"] >= 0
        assert diagnostics["raw_probability"] == pytest.approx(item.raw_probability)
        assert all(value is None or math.isfinite(value) for value in diagnostics.values())
        assert diagnostics["raw_probability_stddev"] == pytest.approx(raw_stddev, abs=1e-6)
        assert diagnostics["calibrated_probability_stddev"] == pytest.approx(calibrated_stddev, abs=1e-6)
        assert diagnostics["calibrated_probability_stddev"] <= diagnostics["raw_probability_stddev"] + 1e-6


def test_soft_saturation_is_smooth_bounded_and_symmetric():
    values = [_soft_saturate(raw, 0.3) for raw in (0.35, 0.60, 0.90)]
    assert values[0] == pytest.approx(0.3 * __import__("math").tanh(0.35 / 0.3))
    assert values[0] < values[1] < values[2] < 0.3
    assert _soft_saturate(-0.6, 0.3) == pytest.approx(-_soft_saturate(0.6, 0.3))
    assert _soft_saturate(0.01, 0.3) == pytest.approx(0.01, rel=0.002)


def test_factor_impact_uses_explicit_groups_and_flags_unknown_factors():
    assert _factor_impact("長期休養明け -0.08").group == "ROTATION"
    assert _factor_impact("市場支持シェア上位 +0.08").group == "MARKET"
    assert _factor_impact("距離替わり200m -0.06").group == "DISTANCE"
    assert _factor_impact("未知の補正 +0.03") is None


def test_score_prediction_records_both_saturation_diagnostics():
    predictor = RuleBasedPredictor()
    features = [_feature(1, 0.7), _feature(2, 0.4)]
    predictions = predictor.predict(features)
    prediction = next(item for item in predictions if item.horse_number == 1)
    assert prediction.score_stability_mode == "hard_cap"
    assert prediction.hard_cap_final_score is not None
    assert prediction.soft_saturation_final_score is not None
    assert prediction.raw_group_adjustments["FORM"] == pytest.approx(0.32)
    assert prediction.hard_cap_final_score != prediction.soft_saturation_final_score
    assert sum(item.soft_saturation_raw_probability for item in predictions) == pytest.approx(1.0)
    assert sum(item.soft_saturation_calibrated_probability for item in predictions) == pytest.approx(1.0)

    challenger = RuleBasedPredictor(stability_config=StabilityConfig(
        use_soft_saturation=True, use_composite_form_market=True,
    )).predict(features)
    challenger_by_horse = {item.horse_id: item for item in challenger}
    for item in predictions:
        candidate = challenger_by_horse[item.horse_id]
        assert item.soft_saturation_raw_probability == pytest.approx(candidate.raw_probability)
        assert item.soft_saturation_calibrated_probability == pytest.approx(candidate.calibrated_probability)


def test_walk_forward_score_comparison_does_not_promote_with_too_few_races():
    rows = []
    for race_number in range(4):
        for horse_number, probability, won in ((1, 0.7, 1), (2, 0.3, 0)):
            rows.append({
                "race_id": f"race-{race_number}",
                "race_date": f"2026-01-0{race_number + 1}",
                "horse_id": f"horse-{horse_number}",
                "horse_number": horse_number,
                "horse_name": f"Horse {horse_number}",
                "prediction_timestamp": "2026-01-01T00:00:00+00:00",
                "features": {
                    "scratched": False,
                    "horse_number": horse_number,
                    "frame_number": horse_number,
                    "distance": 2000,
                    "popularity": horse_number,
                },
                "finish_position": 1 if won else 2,
                "actual_win": float(won),
                "actual_top3": True,
            })
    result = evaluate_score_stability(rows, fold_count=3)
    assert result["validation_scheme"] == "chronological_walk_forward_folds"
    assert [fold["from"] for fold in result["folds"]] == ["2026-01-01", "2026-01-03", "2026-01-04"]
    assert result["status"] == "insufficient_data"
    assert result["promotion_approved"] is False


def test_analysis_rejects_post_start_prediction_when_enforced(tmp_path):
    service = RaceAnalysisService(provider=MockProvider(), database_url=f"sqlite:///{tmp_path}/late.sqlite3")

    with pytest.raises(ValueError, match="発走後"):
        service.analyze(
            AnalysisRequest(
                "2026-08-16",
                "札幌",
                11,
                enforce_pre_race=True,
                execution_time=datetime(2026, 8, 16, 7, 0, tzinfo=timezone.utc),
            )
        )
def test_factor_adjustment_changes_matching_score_factor():
    base_features = {
        "horse_number": 1,
        "frame_number": 1,
        "distance": 2000,
        "distance_change": "same",
        "distance_change_abs": 0,
        "surface_change": "same",
        "popularity": 1,
        "scratched": False,
    }
    item = HorseFeature(
        race_id="race",
        horse_id="adjusted",
        horse_number=1,
        horse_name="Adjusted",
        features=base_features,
        unavailable_features=[],
        as_of="2026-08-15T00:00:00+00:00",
    )

    base_score, base_factors = RuleBasedPredictor()._score_breakdown(item)
    adjusted_score, adjusted_factors = RuleBasedPredictor({"市場人気上位": -0.03})._score_breakdown(item)

    assert "市場人気上位 +0.12" in base_factors
    assert abs(adjusted_score - (base_score - 0.03)) < 0.000001
    assert "学習補正 市場人気上位 -0.03" in adjusted_factors


def test_analysis_service_does_not_apply_unpromoted_factor_challenger(tmp_path):
    database_url = f"sqlite:///{tmp_path}/learned-adjustment.sqlite3"
    with make_session_factory(database_url)() as session:
        session.add(
            PredictionReview(
                race_id="race-factor-ready",
                payload={
                    "factor_attribution": {
                        "overvalued_positive_factors": [
                            {
                                "factor_key": "近走複勝率",
                                "factor": "近走複勝率 +0.36",
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
                                    "factor_key": "近走複勝率",
                                    "factor": "近走複勝率 +0.36",
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

    service = RaceAnalysisService(provider=MockProvider(), database_url=database_url)
    result = service.analyze(AnalysisRequest("2026-08-16", "札幌", 11, iterations=1000, save=False))

    assert all(
        "学習補正 近走複勝率 -0.03" not in prediction.explanation["score_factors"]
        for prediction in result.predictions
    )


def test_analysis_does_not_rebuild_training_data_or_fit_models(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="services.app.orchestrator")
    monkeypatch.setenv("MODEL_ARTIFACT_DIR", str(tmp_path / "models"))

    def fail_training(*_args, **_kwargs):
        raise AssertionError("training work must not run during prediction")

    monkeypatch.setattr(RaceRepository, "training_dataset", fail_training)
    monkeypatch.setattr(LogisticBaselineTrainer, "fit", fail_training)
    service = RaceAnalysisService(provider=MockProvider(), database_url=f"sqlite:///{tmp_path}/analysis.sqlite3")

    result = service.analyze(AnalysisRequest("2026-08-16", "札幌", 11, iterations=1000, save=False))

    assert result.predictions
    assert "RuleBased予測" in result.message
    assert "logistic_predict=" in caplog.text
    assert "training_dataset=" not in caplog.text


def test_saved_logistic_models_are_scored_as_shadow_without_changing_champion(tmp_path, monkeypatch):
    artifact_dir = tmp_path / "models"
    monkeypatch.setenv("MODEL_ARTIFACT_DIR", str(artifact_dir))
    dataset = {"rows": _training_rows_for_blend(target_rows=False)}
    for target in ("actual_win", "actual_top3"):
        trainer = LogisticBaselineTrainer(TrainingConfig(label=target))
        metrics, model = trainer.fit(dataset)
        assert model is not None
        save_logistic_artifact(model, target, "test-feature", metrics, artifact_dir)

    service = RaceAnalysisService(provider=MockProvider(), database_url=f"sqlite:///{tmp_path}/ml-blend.sqlite3")
    features = [_feature(1, 0.7), _feature(2, 0.2)]
    predictions = [_prediction(1, 0.55), _prediction(2, 0.45)]

    shadow, training_result = service._predict_with_saved_logistic_models(features, predictions)

    assert training_result["status"] == "shadow_generated"
    assert training_result["application_status"] == "shadow_only"
    assert {row["target"] for row in shadow} == {"actual_win", "actual_top3"}
    assert all(row["model_version"] == "logistic-v1" for row in shadow)
    assert [prediction.win_probability for prediction in predictions] == [0.55, 0.45]
    assert [prediction.model_version for prediction in predictions] == ["test", "test"]


def test_distance_change_penalty_scales_by_meters():
    predictor = RuleBasedPredictor()

    base_features = {
        "horse_number": 1,
        "frame_number": 1,
        "distance": 2000,
        "distance_change": "same",
        "distance_change_abs": 0,
        "surface_change": "same",
        "scratched": False,
    }
    small_change = HorseFeature(
        race_id="race",
        horse_id="small",
        horse_number=1,
        horse_name="Small",
        features={**base_features, "distance_change": "extend", "distance_change_abs": 200},
        unavailable_features=[],
        as_of="2026-08-15T00:00:00+00:00",
    )
    large_change = HorseFeature(
        race_id="race",
        horse_id="large",
        horse_number=1,
        horse_name="Large",
        features={**base_features, "distance_change": "extend", "distance_change_abs": 800},
        unavailable_features=[],
        as_of="2026-08-15T00:00:00+00:00",
    )

    small_score, small_factors = predictor._score_breakdown(small_change)
    large_score, large_factors = predictor._score_breakdown(large_change)

    assert small_score > large_score
    assert "距離替わり200m -0.03" in small_factors
    assert "距離替わり800m -0.08" in large_factors


def _feature(horse_number: int, top3_rate: float) -> HorseFeature:
    return HorseFeature(
        race_id="target",
        horse_id=f"h{horse_number}",
        horse_number=horse_number,
        horse_name=f"Horse {horse_number}",
        features={"scratched": False, "past_5_top3_rate": top3_rate, "surface": "芝"},
        unavailable_features=[],
        as_of="2026-08-16T00:00:00+00:00",
    )


def _prediction(horse_number: int, win_probability: float) -> PredictionData:
    return PredictionData(
        race_id="target",
        horse_id=f"h{horse_number}",
        horse_number=horse_number,
        horse_name=f"Horse {horse_number}",
        base_score=1.0,
        win_probability=win_probability,
        top2_probability=0.7,
        top3_probability=0.9,
        uncertainty=0.3,
        confidence=0.7,
        explanation={"positive": [], "negative": [], "score_factors": []},
        model_version="test",
        feature_version="test",
    )


def test_review_biggest_ai_misses_prioritize_full_finish_rank_error():
    probabilities = [0.20, 0.17, 0.14, 0.12, 0.10, 0.08, 0.07, 0.05, 0.04, 0.03]
    predictions = [
        _prediction(number, probability).model_copy(
            update={
                "final_rank": number,
                "explanation": {
                    "positive": [],
                    "negative": [],
                    "score_factors": ["市場支持 +0.10"] if number == 5 else [],
                },
            }
        )
        for number, probability in enumerate(probabilities, start=1)
    ]
    actual_positions = {1: 2, 2: 1, 3: 3, 4: 4, 5: 10, 6: 5, 7: 6, 8: 7, 9: 8, 10: 9}
    result = RaceResultData(
        race_id="target",
        result_timestamp="2026-08-30T00:00:00+00:00",
        entries=[
            RaceResultEntryData(
                horse_number=number,
                horse_name=f"Horse {number}",
                finish_position=actual_positions[number],
            )
            for number in range(1, 11)
        ],
    )

    review = PredictionReviewer().review(predictions, result)

    biggest = review["insights"]["biggest_ai_misses"][0]
    assert biggest["horse_number"] == 5
    assert biggest["predicted_rank"] == 5
    assert biggest["finish_position"] == 10
    assert biggest["rank_error"] == 5
    assert review["factor_attribution"]["rank_overvalued_positive_factors"][0]["factor_key"] == "市場支持"
    row = next(item for item in review["rows"] if item["horse_number"] == 5)
    assert row["absolute_finish_rank_error"] == 5
    assert row["rank_error_direction"] == "overvalued"


def test_review_top5_metrics_include_ties_and_keep_incomplete_results_unknown():
    predictions = [
        _prediction(number, 1 / 8).model_copy(update={"final_rank": number})
        for number in range(1, 9)
    ]
    result = RaceResultData(
        race_id="target",
        result_timestamp="2026-08-30T00:00:00+00:00",
        entries=[
            RaceResultEntryData(horse_number=number, horse_name=f"Horse {number}", finish_position=position)
            for number, position in {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 5, 7: 7}.items()
        ],
    )

    metrics = PredictionReviewer().review(predictions, result)["ranking_metrics"]

    assert metrics["winner_in_top5"] is True
    assert metrics["top3_in_predicted_top5"] == 3
    assert metrics["top5_capture_count"] == 5  # both fifth-place horses are actual Top5
    assert metrics["top5_capture_rate"] == 1.0
    assert metrics["top5_capture_4of5"] is True
    assert metrics["top5_capture_5of5"] is True

    incomplete = PredictionReviewer().review(predictions[:4], result)["ranking_metrics"]
    assert incomplete["top5_capture_count"] is None
    assert incomplete["winner_in_top5"] is None


def _training_rows_for_blend(target_rows: bool, race_id: str | None = None) -> list[dict]:
    rows = []
    for index in range(32):
        actual_win = 1.0 if index % 8 == 0 else 0.0
        actual_top3 = index % 8 < 3
        current_race_id = race_id or f"learned-{index // 8}"
        rows.append(
            {
                "race_id": current_race_id,
                "race_date": f"2026-08-{10 + index // 8:02d}",
                "ai_win_probability": 0.30 if actual_win else 0.06,
                "ai_top2_probability": 0.50 if actual_win else 0.20,
                "ai_top3_probability": 0.70 if actual_win else 0.30,
                "base_score": 2.0 if actual_win else 1.1,
                "confidence": 0.6,
                "actual_win": actual_win,
                "actual_top3": actual_top3,
                "features": {
                    "past_5_top3_rate": 0.8 if actual_win else 0.2,
                    "surface": "芝",
                    "running_style": "front" if actual_win else "closer",
                },
            }
        )
    if target_rows:
        rows.append(
            {
                "race_id": "target",
                "ai_win_probability": 0.99,
                "ai_top2_probability": 0.99,
                "ai_top3_probability": 0.99,
                "base_score": 9.0,
                "confidence": 0.9,
                "actual_win": 1.0,
                "actual_top3": True,
                "features": {"past_5_top3_rate": 1.0, "surface": "芝", "running_style": "front"},
            }
        )
    return rows


def test_carried_weight_change_affects_score():
    predictor = RuleBasedPredictor()

    base_features = {
        "horse_number": 1,
        "frame_number": 1,
        "distance": 2000,
        "distance_change": "same",
        "distance_change_abs": 0,
        "surface_change": "same",
        "scratched": False,
    }
    heavier = HorseFeature(
        race_id="race",
        horse_id="heavier",
        horse_number=1,
        horse_name="Heavier",
        features={**base_features, "carried_weight_change_from_last": 3.0},
        unavailable_features=[],
        as_of="2026-08-15T00:00:00+00:00",
    )
    lighter = HorseFeature(
        race_id="race",
        horse_id="lighter",
        horse_number=1,
        horse_name="Lighter",
        features={**base_features, "carried_weight_change_from_last": -3.0},
        unavailable_features=[],
        as_of="2026-08-15T00:00:00+00:00",
    )

    heavier_score, heavier_factors = predictor._score_breakdown(heavier)
    lighter_score, lighter_factors = predictor._score_breakdown(lighter)

    assert lighter_score > heavier_score
    assert "前走比斤量+3.0kg -0.04" in heavier_factors
    assert "前走比斤量-3.0kg +0.04" in lighter_factors


def test_grade_step_and_graded_results_affect_score():
    predictor = RuleBasedPredictor()

    base_features = {
        "horse_number": 1,
        "frame_number": 1,
        "distance": 2000,
        "distance_change": "same",
        "distance_change_abs": 0,
        "surface_change": "same",
        "scratched": False,
        "recent_graded_starts": 2,
        "recent_graded_top3_rate": 0.5,
    }
    class_up = HorseFeature(
        race_id="race",
        horse_id="up",
        horse_number=1,
        horse_name="Up",
        features={**base_features, "grade_step_from_last": 1},
        unavailable_features=[],
        as_of="2026-08-15T00:00:00+00:00",
    )
    class_down = HorseFeature(
        race_id="race",
        horse_id="down",
        horse_number=1,
        horse_name="Down",
        features={**base_features, "grade_step_from_last": -1},
        unavailable_features=[],
        as_of="2026-08-15T00:00:00+00:00",
    )

    class_up_score, class_up_factors = predictor._score_breakdown(class_up)
    class_down_score, class_down_factors = predictor._score_breakdown(class_down)

    assert class_down_score > class_up_score
    assert "近走重賞経験 +0.03" in class_up_factors
    assert "重賞好走率 +0.04" in class_up_factors
    assert "格上挑戦 -0.03" in class_up_factors
    assert "相手関係緩和 +0.04" in class_down_factors


def test_adjusted_recent_speed_affects_score():
    predictor = RuleBasedPredictor()

    base_features = {
        "horse_number": 1,
        "frame_number": 1,
        "distance": 2000,
        "distance_change": "same",
        "distance_change_abs": 0,
        "surface_change": "same",
        "scratched": False,
    }
    fast = HorseFeature(
        race_id="race",
        horse_id="fast",
        horse_number=1,
        horse_name="Fast",
        features={**base_features, "weighted_adjusted_speed_delta_mps": 0.3},
        unavailable_features=[],
        as_of="2026-08-15T00:00:00+00:00",
    )
    slow = HorseFeature(
        race_id="race",
        horse_id="slow",
        horse_number=1,
        horse_name="Slow",
        features={**base_features, "weighted_adjusted_speed_delta_mps": -0.25},
        unavailable_features=[],
        as_of="2026-08-15T00:00:00+00:00",
    )

    fast_score, fast_factors = predictor._score_breakdown(fast)
    slow_score, _ = predictor._score_breakdown(slow)

    assert fast_score > slow_score
    assert "補正後走破速度 +0.05" in fast_factors


def test_distance_band_fit_affects_score():
    predictor = RuleBasedPredictor()

    base_features = {
        "horse_number": 1,
        "frame_number": 1,
        "distance": 2000,
        "distance_change": "same",
        "distance_change_abs": 0,
        "surface_change": "same",
        "scratched": False,
    }
    strong_fit = HorseFeature(
        race_id="race",
        horse_id="strong",
        horse_number=1,
        horse_name="Strong",
        features={**base_features, "same_distance_band_top3_rate": 0.8, "same_surface_distance_band_top3_rate": 0.8},
        unavailable_features=[],
        as_of="2026-08-15T00:00:00+00:00",
    )
    weak_fit = HorseFeature(
        race_id="race",
        horse_id="weak",
        horse_number=1,
        horse_name="Weak",
        features={**base_features, "same_distance_band_top3_rate": 0.1, "same_surface_distance_band_top3_rate": 0.1},
        unavailable_features=[],
        as_of="2026-08-15T00:00:00+00:00",
    )

    strong_score, strong_factors = predictor._score_breakdown(strong_fit)
    weak_score, _ = predictor._score_breakdown(weak_fit)

    assert strong_score > weak_score
    assert "同距離帯適性 +0.05" in strong_factors
    assert "同馬場距離帯適性 +0.05" in strong_factors


def test_recent_body_weight_change_affects_score():
    predictor = RuleBasedPredictor()

    base_features = {
        "horse_number": 1,
        "frame_number": 1,
        "distance": 2000,
        "distance_change": "same",
        "distance_change_abs": 0,
        "surface_change": "same",
        "scratched": False,
    }
    stable = HorseFeature(
        race_id="race",
        horse_id="stable",
        horse_number=1,
        horse_name="Stable",
        features={**base_features, "latest_recent_horse_weight_change": -4},
        unavailable_features=[],
        as_of="2026-08-15T00:00:00+00:00",
    )
    sharp_drop = HorseFeature(
        race_id="race",
        horse_id="drop",
        horse_number=1,
        horse_name="Drop",
        features={**base_features, "latest_recent_horse_weight_change": -14},
        unavailable_features=[],
        as_of="2026-08-15T00:00:00+00:00",
    )

    stable_score, stable_factors = predictor._score_breakdown(stable)
    sharp_drop_score, sharp_drop_factors = predictor._score_breakdown(sharp_drop)

    assert stable_score > sharp_drop_score
    assert "近走馬体重安定 +0.02" in stable_factors
    assert "近走馬体重急減 -0.04" in sharp_drop_factors


def test_course_frame_bias_affects_score():
    predictor = RuleBasedPredictor()

    base_features = {
        "horse_number": 1,
        "distance": 2000,
        "distance_change": "same",
        "distance_change_abs": 0,
        "surface_change": "same",
        "scratched": False,
    }
    inner = HorseFeature(
        race_id="race",
        horse_id="inner",
        horse_number=1,
        horse_name="Inner",
        features={**base_features, "frame_number": 1, "course_frame_bias": 0.04},
        unavailable_features=[],
        as_of="2026-08-15T00:00:00+00:00",
    )
    outer = HorseFeature(
        race_id="race",
        horse_id="outer",
        horse_number=1,
        horse_name="Outer",
        features={**base_features, "frame_number": 8, "course_frame_bias": -0.03},
        unavailable_features=[],
        as_of="2026-08-15T00:00:00+00:00",
    )

    inner_score, inner_factors = predictor._score_breakdown(inner)
    outer_score, outer_factors = predictor._score_breakdown(outer)

    assert inner_score > outer_score
    assert "コース枠補正 +0.04" in inner_factors
    assert "コース枠補正 -0.03" in outer_factors


def test_age_band_affects_score():
    predictor = RuleBasedPredictor()

    base_features = {
        "horse_number": 1,
        "frame_number": 1,
        "distance": 2000,
        "distance_change": "same",
        "distance_change_abs": 0,
        "surface_change": "same",
        "scratched": False,
    }
    prime = HorseFeature(
        race_id="race",
        horse_id="prime",
        horse_number=1,
        horse_name="Prime",
        features={**base_features, "age_band": "prime"},
        unavailable_features=[],
        as_of="2026-08-15T00:00:00+00:00",
    )
    senior = HorseFeature(
        race_id="race",
        horse_id="senior",
        horse_number=1,
        horse_name="Senior",
        features={**base_features, "age_band": "senior"},
        unavailable_features=[],
        as_of="2026-08-15T00:00:00+00:00",
    )

    prime_score, prime_factors = predictor._score_breakdown(prime)
    senior_score, senior_factors = predictor._score_breakdown(senior)

    assert prime_score > senior_score
    assert "年齢充実期 +0.04" in prime_factors
    assert "年齢高齢 -0.06" in senior_factors


def test_recent_consistency_affects_score():
    predictor = RuleBasedPredictor()

    base_features = {
        "horse_number": 1,
        "frame_number": 1,
        "distance": 2000,
        "distance_change": "same",
        "distance_change_abs": 0,
        "surface_change": "same",
        "scratched": False,
        "past_5_top3_rate": 0.5,
    }
    stable = HorseFeature(
        race_id="race",
        horse_id="stable",
        horse_number=1,
        horse_name="Stable",
        features={**base_features, "recent_consistency_label": "stable"},
        unavailable_features=[],
        as_of="2026-08-15T00:00:00+00:00",
    )
    volatile = HorseFeature(
        race_id="race",
        horse_id="volatile",
        horse_number=1,
        horse_name="Volatile",
        features={**base_features, "recent_consistency_label": "volatile"},
        unavailable_features=[],
        as_of="2026-08-15T00:00:00+00:00",
    )

    stable_score, stable_factors = predictor._score_breakdown(stable)
    volatile_score, volatile_factors = predictor._score_breakdown(volatile)

    assert stable_score > volatile_score
    assert "近走安定 +0.04" in stable_factors
    assert "近走振れ幅大 -0.04" in volatile_factors


def test_last_run_content_affects_score():
    predictor = RuleBasedPredictor()

    base_features = {
        "horse_number": 1,
        "frame_number": 1,
        "distance": 2000,
        "distance_change": "same",
        "distance_change_abs": 0,
        "surface_change": "same",
        "scratched": False,
    }
    strong_last = HorseFeature(
        race_id="race",
        horse_id="strong-last",
        horse_number=1,
        horse_name="StrongLast",
        features={**base_features, "last_run_top3": True, "last_run_finish_ratio": 0.2},
        unavailable_features=[],
        as_of="2026-08-15T00:00:00+00:00",
    )
    weak_last = HorseFeature(
        race_id="race",
        horse_id="weak-last",
        horse_number=1,
        horse_name="WeakLast",
        features={**base_features, "last_run_top3": False, "last_run_finish_ratio": 0.8},
        unavailable_features=[],
        as_of="2026-08-15T00:00:00+00:00",
    )

    strong_score, strong_factors = predictor._score_breakdown(strong_last)
    weak_score, weak_factors = predictor._score_breakdown(weak_last)

    assert strong_score > weak_score
    assert "前走複勝圏 +0.04" in strong_factors
    assert "前走頭数補正 +0.03" in strong_factors
    assert "前走大敗気味 -0.05" in weak_factors


def test_field_relative_record_affects_score():
    predictor = RuleBasedPredictor()

    base_features = {
        "horse_number": 1,
        "frame_number": 1,
        "distance": 2000,
        "distance_change": "same",
        "distance_change_abs": 0,
        "surface_change": "same",
        "scratched": False,
    }
    field_top = HorseFeature(
        race_id="race",
        horse_id="field-top",
        horse_number=1,
        horse_name="FieldTop",
        features={
            **base_features,
            "field_total_prize_money_rank_ratio": 0.0,
            "field_career_win_rate_rank_ratio": 0.0,
            "field_career_top3_rate_rank_ratio": 0.0,
        },
        unavailable_features=[],
        as_of="2026-08-15T00:00:00+00:00",
    )
    field_low = HorseFeature(
        race_id="race",
        horse_id="field-low",
        horse_number=1,
        horse_name="FieldLow",
        features={
            **base_features,
            "field_total_prize_money_rank_ratio": 1.0,
            "field_career_win_rate_rank_ratio": 1.0,
            "field_career_top3_rate_rank_ratio": 1.0,
        },
        unavailable_features=[],
        as_of="2026-08-15T00:00:00+00:00",
    )

    top_score, top_factors = predictor._score_breakdown(field_top)
    low_score, low_factors = predictor._score_breakdown(field_low)

    assert top_score > low_score
    assert "メンバー内賞金上位 +0.06" in top_factors
    assert "メンバー内通算勝率上位 +0.03" in top_factors
    assert "メンバー内通算複勝率上位 +0.04" in top_factors
    assert "メンバー内賞金下位 -0.03" in low_factors
    assert "メンバー内通算複勝率下位 -0.03" in low_factors


def test_field_market_support_affects_score():
    predictor = RuleBasedPredictor()

    base_features = {
        "horse_number": 1,
        "frame_number": 1,
        "distance": 2000,
        "distance_change": "same",
        "distance_change_abs": 0,
        "surface_change": "same",
        "scratched": False,
    }
    market_top = HorseFeature(
        race_id="race",
        horse_id="market-top",
        horse_number=1,
        horse_name="MarketTop",
        features={
            **base_features,
            "field_implied_win_probability_rank_ratio": 0.0,
            "field_implied_win_probability_share": 0.24,
        },
        unavailable_features=[],
        as_of="2026-08-15T00:00:00+00:00",
    )
    market_low = HorseFeature(
        race_id="race",
        horse_id="market-low",
        horse_number=1,
        horse_name="MarketLow",
        features={
            **base_features,
            "field_implied_win_probability_rank_ratio": 1.0,
            "field_implied_win_probability_share": 0.03,
        },
        unavailable_features=[],
        as_of="2026-08-15T00:00:00+00:00",
    )

    top_score, top_factors = predictor._score_breakdown(market_top)
    low_score, low_factors = predictor._score_breakdown(market_low)

    assert top_score > low_score
    assert "市場支持シェア上位 +0.08" in top_factors
    assert "メンバー内市場上位 +0.04" in top_factors
    assert "市場支持シェア下位 -0.03" in low_factors
    assert "メンバー内市場下位 -0.03" in low_factors
