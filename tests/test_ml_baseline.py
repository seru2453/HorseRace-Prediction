from services.common.models import HorseFeature, PredictionData
from services.ml.baseline import (
    LogisticBaselineTrainer,
    TrainingConfig,
    _race_rank_metrics,
    prediction_rows_from_current,
    temporal_race_split,
)


def test_logistic_evaluation_records_actual_top5_capture_rates():
    rows = [
        {"race_id": "r1", "actual_win": index == 0, "actual_top3": index < 3, "finish_position": index + 1}
        for index in range(7)
    ]
    metrics = _race_rank_metrics(rows, [0.7, 0.1, 0.08, 0.06, 0.04, 0.015, 0.005], rows, [0.7, 0.1, 0.08, 0.06, 0.04, 0.015, 0.005])

    assert metrics["top5_capture"] == 1.0
    assert metrics["top5_capture_4of5_rate"] == 1.0
    assert metrics["top5_capture_5of5_rate"] == 1.0
    assert metrics["top5_complete_race_count"] == 1


def test_logistic_baseline_reports_insufficient_data():
    result = LogisticBaselineTrainer().train({"rows": []})

    assert result["status"] == "insufficient_data"
    assert result["reason"] == "no_rows"


def test_logistic_baseline_trains_from_training_dataset():
    rows = _training_rows()

    result = LogisticBaselineTrainer(TrainingConfig(min_rows=20)).train({"rows": rows})

    assert result["status"] == "trained_in_sample"
    assert result["row_count"] == 32
    assert result["positive_count"] == 4
    assert result["negative_count"] == 28
    assert result["feature_count"] > 0
    assert result["brier_score"] < 0.25
    assert result["race_holdout"]["status"] == "evaluated"
    assert result["race_holdout"]["validation_scheme"] == "walk_forward"
    assert result["race_holdout"]["fold_count"] == 2
    assert result["race_holdout"]["blend_approved"] is True
    assert result["race_holdout"]["blended_brier_score"] < result["race_holdout"]["rule_brier_score"]
    assert result["top_positive_coefficients"]


def test_logistic_baseline_rejects_blend_when_rule_probabilities_are_better():
    rows = []
    for race_index in range(4):
        for horse_index in range(8):
            actual_win = 1.0 if horse_index == 0 else 0.0
            rows.append(
                {
                    "race_id": f"race-{race_index}",
                    "ai_win_probability": 0.999 if actual_win else 0.00014,
                    "ai_top2_probability": 0.999 if actual_win else 0.001,
                    "ai_top3_probability": 0.999 if actual_win else 0.002,
                    "base_score": 1.0,
                    "confidence": 0.6,
                    "actual_win": actual_win,
                    "features": {"surface": "芝"},
                }
            )

    result = LogisticBaselineTrainer(TrainingConfig(min_rows=20)).train({"rows": rows})

    assert result["race_holdout"]["status"] == "evaluated"
    assert result["race_holdout"]["blend_approved"] is False
    assert result["race_holdout"]["blended_brier_score"] > result["race_holdout"]["rule_brier_score"]


def test_logistic_baseline_requires_meaningful_holdout_improvement():
    trainer = LogisticBaselineTrainer(TrainingConfig(min_brier_improvement=0.001))

    assert trainer._blend_is_approved(0.1000, 0.0995, 0.30, 0.29) is False
    assert trainer._blend_is_approved(0.1000, 0.0980, 0.30, 0.29) is True
    assert trainer._blend_is_approved(0.1000, 0.0980, 0.30, 0.31) is False


def test_logistic_baseline_predicts_rows_and_blends_probabilities():
    trainer = LogisticBaselineTrainer(TrainingConfig(min_rows=20, blend_weight=0.25))
    result, model = trainer.fit({"rows": _training_rows()})

    assert result["status"] == "trained_in_sample"
    assert model is not None

    probabilities = model.predict_rows(
        [
            {
                "race_id": "target",
                "ai_win_probability": 0.2,
                "ai_top2_probability": 0.4,
                "ai_top3_probability": 0.55,
                "base_score": 1.8,
                "confidence": 0.6,
                "features": {"past_5_top3_rate": 0.7, "surface": "芝", "running_style": "front"},
            },
            {
                "race_id": "target",
                "ai_win_probability": 0.1,
                "ai_top2_probability": 0.2,
                "ai_top3_probability": 0.3,
                "base_score": 1.0,
                "confidence": 0.5,
                "features": {"past_5_top3_rate": 0.2, "surface": "芝", "running_style": "closer"},
            },
        ]
    )
    blended = trainer.blend_probabilities([0.6, 0.4], probabilities)

    assert len(probabilities) == 2
    assert probabilities[0] > probabilities[1]
    assert len(blended) == 2
    assert 0.999 <= sum(blended) <= 1.001
    assert blended[0] > blended[1]


def test_logistic_baseline_ignores_identity_categories_and_imputes_missing_values():
    trainer = LogisticBaselineTrainer(TrainingConfig(min_rows=20))
    rows = _training_rows()
    for index, row in enumerate(rows):
        row["features"].update(
            {
                "sire_name": f"sire-{index}",
                "horse_number": index + 1,
                "past_5_top3_rate": None if index % 3 == 0 else row["features"].get("past_5_top3_rate"),
            }
        )

    result, model = trainer.fit({"rows": rows})

    assert model is not None
    assert "feature__sire_name" not in model.columns
    assert "feature__horse_number" not in model.columns
    probabilities = model.predict_rows(
        [{"ai_win_probability": 0.2, "features": {"past_5_top3_rate": None}}]
    )
    assert len(probabilities) == 1
    assert 0.0 <= probabilities[0] <= 1.0


def test_logistic_baseline_rejects_rank_accuracy_regression():
    trainer = LogisticBaselineTrainer(TrainingConfig(min_brier_improvement=0.001))

    assert trainer._blend_is_approved(0.1, 0.098, 0.3, 0.29, 0.3, 0.2, 0.6, 0.6) is False
    assert trainer._blend_is_approved(0.1, 0.098, 0.3, 0.29, 0.3, 0.3, 0.6, 0.5) is False


def test_prediction_rows_from_current_keeps_prediction_order_and_features():
    features = [
        HorseFeature(
            race_id="race",
            horse_id="h1",
            horse_number=1,
            horse_name="One",
            features={"scratched": False, "past_5_top3_rate": 0.5},
            unavailable_features=[],
            as_of="2026-08-16T00:00:00+00:00",
        ),
        HorseFeature(
            race_id="race",
            horse_id="h2",
            horse_number=2,
            horse_name="Two",
            features={"scratched": False, "past_5_top3_rate": 0.2},
            unavailable_features=[],
            as_of="2026-08-16T00:00:00+00:00",
        ),
    ]
    predictions = [
        _prediction(2, "Two", 0.55),
        _prediction(1, "One", 0.45),
    ]

    rows = prediction_rows_from_current(features, predictions)

    assert [row["horse_number"] for row in rows] == [2, 1]
    assert rows[0]["features"]["past_5_top3_rate"] == 0.2


def test_temporal_split_keeps_races_disjoint_and_ordered():
    rows = [
        {"race_id": f"race-{race}", "race_date": f"2026-08-{race + 1:02d}", "horse_id": f"h-{race}-{horse}"}
        for race in range(10)
        for horse in range(4)
    ]

    split = temporal_race_split(rows)
    train_ids = set(split["train_race_ids"])
    validation_ids = set(split["validation_race_ids"])
    test_ids = set(split["test_race_ids"])

    assert train_ids.isdisjoint(validation_ids | test_ids)
    assert validation_ids.isdisjoint(test_ids)
    assert max(row["race_date"] for row in split["train"]) < min(
        row["race_date"] for row in split["validation"]
    )
    assert max(row["race_date"] for row in split["validation"]) < min(
        row["race_date"] for row in split["test"]
    )


def test_logistic_predictions_are_reproducible():
    trainer = LogisticBaselineTrainer(TrainingConfig(min_rows=20))
    _, first = trainer.fit({"rows": _training_rows()})
    _, second = trainer.fit({"rows": _training_rows()})
    current = [{"ai_win_probability": 0.2, "features": {"past_5_top3_rate": 0.5}}]

    assert first is not None and second is not None
    assert first.predict_rows(current) == second.predict_rows(current)


def _training_rows():
    rows = []
    for index in range(32):
        actual_win = 1.0 if index % 8 == 0 else 0.0
        rows.append(
            {
                "race_id": f"race-{index // 8}",
                "race_date": f"2026-08-{10 + index // 8:02d}",
                "ai_win_probability": 0.30 if actual_win else 0.06,
                "ai_top2_probability": 0.50 if actual_win else 0.20,
                "ai_top3_probability": 0.70 if actual_win else 0.30,
                "base_score": 2.0 if actual_win else 1.1,
                "confidence": 0.6,
                "actual_win": actual_win,
                "features": {
                    "past_5_top3_rate": 0.8 if actual_win else 0.2,
                    "surface": "芝",
                    "running_style": "front" if actual_win else "closer",
                },
            }
        )
    return rows


def _prediction(horse_number: int, horse_name: str, win_probability: float) -> PredictionData:
    return PredictionData(
        race_id="race",
        horse_id=f"h{horse_number}",
        horse_number=horse_number,
        horse_name=horse_name,
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
