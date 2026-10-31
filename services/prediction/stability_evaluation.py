from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
import math
from statistics import pstdev

from services.common.models import HorseFeature
from services.prediction.config import DEFAULT_STABILITY_CONFIG
from services.prediction.rule_based import RuleBasedPredictor


MIN_PROMOTION_RACES = 30
MIN_PROMOTION_FOLDS = 5


def evaluate_score_stability(rows: list[dict], fold_count: int = 5) -> dict:
    """Compare hard-cap Champion and soft-saturation challenger on prediction-time snapshots."""
    races: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row.get("features") is None or row.get("finish_position") is None:
            continue
        races[str(row["race_id"])].append(row)
    ordered = sorted(
        races.items(),
        key=lambda pair: (str(pair[1][0].get("race_date") or ""), pair[0]),
    )
    if not ordered:
        return {"status": "insufficient_data", "race_count": 0, "folds": [], "promotion_approved": False}

    challenger_config = replace(
        DEFAULT_STABILITY_CONFIG,
        use_soft_saturation=True,
        use_composite_form_market=True,
    )
    predictors = {
        "hard_cap": RuleBasedPredictor(stability_config=DEFAULT_STABILITY_CONFIG),
        "soft_saturation": RuleBasedPredictor(stability_config=challenger_config),
    }
    evaluated = []
    for race_id, race_rows in ordered:
        features = [
            HorseFeature(
                race_id=race_id,
                horse_id=str(row["horse_id"]),
                horse_number=int(row["horse_number"]),
                horse_name=str(row.get("horse_name") or ""),
                features=row["features"],
                unavailable_features=[],
                as_of=str(row.get("prediction_timestamp") or ""),
            )
            for row in race_rows
        ]
        labels = {str(row["horse_id"]): row for row in race_rows}
        outputs = {name: predictor.predict(features) for name, predictor in predictors.items()}
        evaluated.append((race_id, race_rows[0].get("race_date"), labels, outputs))

    by_date: list[list] = []
    for race in evaluated:
        race_date = str(race[1] or "")
        if not by_date or str(by_date[-1][0][1] or "") != race_date:
            by_date.append([])
        by_date[-1].append(race)
    fold_count = max(1, min(int(fold_count), len(by_date)))
    folds = []
    base, remainder = divmod(len(by_date), fold_count)
    offset = 0
    for fold_index in range(fold_count):
        date_group_size = base + (1 if fold_index < remainder else 0)
        fold_rows = [
            race
            for date_group in by_date[offset:offset + date_group_size]
            for race in date_group
        ]
        offset += date_group_size
        folds.append({
            "fold": fold_index + 1,
            "from": str(fold_rows[0][1]),
            "to": str(fold_rows[-1][1]),
            "race_count": len(fold_rows),
            "metrics": {
                name: _metrics(fold_rows, name)
                for name in predictors
            },
        })

    totals = {name: _metrics(evaluated, name) for name in predictors}
    enough_data = len(evaluated) >= MIN_PROMOTION_RACES and fold_count >= MIN_PROMOTION_FOLDS
    hard, soft = totals["hard_cap"], totals["soft_saturation"]
    no_regression = all(
        soft[key] <= hard[key]
        for key in ("multiclass_brier", "winner_log_loss", "mean_winner_rank", "mean_rank_error")
    ) and all(soft[key] >= hard[key] for key in ("top3_capture", "top5_capture"))
    spread_improved = soft["mean_final_score_stddev"] > hard["mean_final_score_stddev"]
    return {
        "status": "evaluated" if enough_data else "insufficient_data",
        "validation_scheme": "chronological_walk_forward_folds",
        "race_count": len(evaluated),
        "fold_count": fold_count,
        "minimum_promotion_races": MIN_PROMOTION_RACES,
        "minimum_promotion_folds": MIN_PROMOTION_FOLDS,
        "metrics": totals,
        "folds": folds,
        "promotion_approved": bool(enough_data and no_regression and spread_improved),
        "promotion_reason": (
            "all safety metrics passed and score spread improved"
            if enough_data and no_regression and spread_improved
            else "Champion retained: insufficient races or challenger did not pass every safety metric"
        ),
    }


def _metrics(races: list, predictor_name: str) -> dict:
    brier_values = []
    winner_losses = []
    winner_ranks = []
    top3_hits = top3_possible = top5_hits = top5_possible = 0
    race_probability_spreads = []
    race_score_spreads = []
    rank_errors = []
    unclassified_factor_count = 0
    for _, _, labels, outputs in races:
        predictions = outputs[predictor_name]
        by_horse = {prediction.horse_id: prediction for prediction in predictions}
        if not by_horse:
            continue
        probs = [prediction.win_probability for prediction in predictions]
        race_probability_spreads.append(pstdev(probs) if len(probs) > 1 else 0.0)
        scores = [prediction.base_score for prediction in predictions]
        race_score_spreads.append(pstdev(scores) if len(scores) > 1 else 0.0)
        unclassified_factor_count += sum(len(prediction.unclassified_score_factors) for prediction in predictions)
        predicted_ranks = {prediction.horse_id: rank for rank, prediction in enumerate(predictions, start=1)}
        rank_errors.extend(
            abs(predicted_ranks[horse_id] - int(row["finish_position"]))
            for horse_id, row in labels.items()
            if horse_id in predicted_ranks and row.get("finish_position") is not None
        )
        brier_values.extend(
            (prediction.win_probability - float(labels[prediction.horse_id].get("actual_win") or 0.0)) ** 2
            for prediction in predictions if prediction.horse_id in labels
        )
        winners = [row for row in labels.values() if float(row.get("actual_win") or 0.0) == 1.0]
        for winner in winners:
            prediction = by_horse.get(str(winner["horse_id"]))
            if prediction is None:
                continue
            winner_losses.append(-math.log(max(prediction.win_probability, 1e-15)))
            winner_ranks.append(predictions.index(prediction) + 1)
        actual_top3 = {horse_id for horse_id, row in labels.items() if bool(row.get("actual_top3"))}
        predicted3 = {prediction.horse_id for prediction in predictions[:3]}
        predicted5 = {prediction.horse_id for prediction in predictions[:5]}
        top3_hits += len(actual_top3 & predicted3)
        top3_possible += len(actual_top3)
        top5_hits += len(actual_top3 & predicted5)
        top5_possible += len(actual_top3)
    average = lambda values: sum(values) / len(values) if values else 0.0
    return {
        "multiclass_brier": round(average(brier_values), 8),
        "winner_log_loss": round(average(winner_losses), 8),
        "mean_winner_rank": round(average(winner_ranks), 6),
        "mean_rank_error": round(average(rank_errors), 6),
        "top3_capture": round(top3_hits / top3_possible, 6) if top3_possible else 0.0,
        "top5_capture": round(top5_hits / top5_possible, 6) if top5_possible else 0.0,
        "mean_race_probability_stddev": round(average(race_probability_spreads), 8),
        "mean_final_score_stddev": round(average(race_score_spreads), 8),
        "winner_count": len(winner_ranks),
        "unclassified_factor_count": unclassified_factor_count,
    }
