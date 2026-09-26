from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import pandas as pd
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.metrics import brier_score_loss, log_loss, mean_absolute_error
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline, make_pipeline
from sklearn.preprocessing import StandardScaler

from services.common.models import HorseFeature, PredictionData


BASELINE_FEATURES = [
    "ai_win_probability",
    "ai_top2_probability",
    "ai_top3_probability",
    "base_score",
    "confidence",
]

LOGISTIC_BASELINE_VERSION = "logistic-v1"

EXCLUDED_NUMERIC_FEATURES = {
    "feature__horse_number",
    "feature__frame_number",
}


@dataclass(frozen=True)
class TrainingConfig:
    label: str = "actual_win"
    min_rows: int = 20
    min_races: int = 4
    min_holdout_folds: int = 2
    min_positive: int = 2
    min_negative: int = 2
    blend_weight: float = 0.25
    min_brier_improvement: float = 0.001
    include_jev: bool = True


def temporal_race_split(
    rows: list[dict[str, Any]],
    train_ratio: float = 0.70,
    validation_ratio: float = 0.15,
) -> dict[str, list[dict[str, Any]] | list[str]]:
    race_dates: dict[str, str] = {}
    for row in rows:
        race_id = row.get("race_id")
        if race_id is not None:
            race_dates.setdefault(str(race_id), str(row.get("race_date") or race_id))
    race_ids = sorted(race_dates, key=lambda race_id: (race_dates[race_id], race_id))
    if len(race_ids) < 3:
        return {"train": [], "validation": [], "test": [], "train_race_ids": [], "validation_race_ids": [], "test_race_ids": []}
    train_end = max(1, min(len(race_ids) - 2, int(len(race_ids) * train_ratio)))
    validation_end = max(train_end + 1, min(len(race_ids) - 1, int(len(race_ids) * (train_ratio + validation_ratio))))
    partitions = {
        "train_race_ids": race_ids[:train_end],
        "validation_race_ids": race_ids[train_end:validation_end],
        "test_race_ids": race_ids[validation_end:],
    }
    for name in ("train", "validation", "test"):
        allowed = set(partitions[f"{name}_race_ids"])
        partitions[name] = [row for row in rows if str(row.get("race_id")) in allowed]
    return partitions


@dataclass
class TrainedLogisticBaseline:
    model: Pipeline
    columns: list[str]
    config: TrainingConfig

    def predict_rows(self, rows: list[dict[str, Any]]) -> list[float]:
        if not rows:
            return []
        frame = LogisticBaselineTrainer(self.config)._frame(rows)
        X = LogisticBaselineTrainer(self.config)._features(frame)
        X = X.reindex(columns=self.columns)
        return [float(value) for value in self.model.predict_proba(X)[:, 1]]


def prediction_rows_from_current(features: list[HorseFeature], predictions: list[PredictionData]) -> list[dict[str, Any]]:
    features_by_horse = {item.horse_number: item for item in features}
    rows: list[dict[str, Any]] = []
    for prediction in predictions:
        feature = features_by_horse.get(prediction.horse_number)
        if feature is None or feature.features.get("scratched"):
            continue
        rows.append(
            {
                "race_id": prediction.race_id,
                "horse_id": prediction.horse_id,
                "horse_number": prediction.horse_number,
                "horse_name": prediction.horse_name,
                "ai_win_probability": prediction.win_probability,
                "ai_top2_probability": prediction.top2_probability,
                "ai_top3_probability": prediction.top3_probability,
                "base_score": prediction.base_score,
                "confidence": prediction.confidence,
                "features": feature.features,
            }
        )
    return rows


class LogisticBaselineTrainer:
    def __init__(self, config: TrainingConfig | None = None) -> None:
        self.config = config or TrainingConfig()

    def train(self, dataset: dict[str, Any]) -> dict[str, Any]:
        result, _ = self.fit(dataset)
        return result

    def fit(self, dataset: dict[str, Any]) -> tuple[dict[str, Any], TrainedLogisticBaseline | None]:
        rows = dataset.get("rows") or []
        frame = self._frame(rows)
        status = self._data_status(frame)
        if status["status"] != "ok":
            return self._status_result(status, rows), None

        frame = frame.dropna(subset=[self.config.label])
        y = frame[self.config.label].astype(int)
        X = self._features(frame)
        model = self._new_model()
        model.fit(X, y)
        probabilities = model.predict_proba(X)[:, 1]
        logistic_model = model.named_steps["logisticregression"]
        coefficient_columns = model.named_steps["simpleimputer"].get_feature_names_out(X.columns)
        coefficients = self._top_coefficients(coefficient_columns, logistic_model.coef_[0])
        return (
            {
                "status": "trained_in_sample",
                "model_type": "logistic_regression",
                "label": self.config.label,
                "row_count": len(frame),
                "race_count": frame["race_id"].nunique(),
                "positive_count": int(y.sum()),
                "negative_count": int((1 - y).sum()),
                "feature_count": len(coefficient_columns),
                "brier_score": round(float(brier_score_loss(y, probabilities)), 6),
                "log_loss": round(float(log_loss(y, probabilities, labels=[0, 1])), 6),
                "top_positive_coefficients": coefficients["positive"],
                "top_negative_coefficients": coefficients["negative"],
                "race_holdout": self._race_holdout(frame),
                "blend_weight": self.config.blend_weight,
                "note": "logistic-v1はshadow評価専用です。検証結果だけでChampionへ自動昇格しません。",
            },
            TrainedLogisticBaseline(model=model, columns=list(X.columns), config=self.config),
        )

    def blend_probabilities(self, rule_probabilities: list[float], model_probabilities: list[float]) -> list[float]:
        if len(rule_probabilities) != len(model_probabilities):
            return rule_probabilities
        normalized_model = self._normalize(model_probabilities)
        weight = max(0.0, min(0.5, float(self.config.blend_weight)))
        blended = [
            (1.0 - weight) * float(rule_probability) + weight * float(model_probability)
            for rule_probability, model_probability in zip(rule_probabilities, normalized_model, strict=True)
        ]
        return self._normalize(blended)

    def _frame(self, rows: list[dict[str, Any]]) -> pd.DataFrame:
        flat_rows = []
        for row in rows:
            flat = {
                "race_id": row.get("race_id"),
                "race_date": row.get("race_date"),
                "horse_id": row.get("horse_id"),
                "horse_number": row.get("horse_number"),
                "finish_position": row.get("finish_position"),
                "relative_finish": row.get("relative_finish"),
                "actual_win": row.get("actual_win"),
                "actual_top3": row.get("actual_top3"),
                self.config.label: row.get(self.config.label),
            }
            for key in BASELINE_FEATURES:
                flat[key] = row.get(key)
            for key, value in (row.get("features") or {}).items():
                if value is None or isinstance(value, (str, int, float, bool)):
                    flat[f"feature__{key}"] = value
            flat_rows.append(flat)
        frame = pd.DataFrame(flat_rows)
        for key in ["race_id", "race_date", self.config.label, *BASELINE_FEATURES]:
            if key not in frame:
                frame[key] = None
        return frame

    def _status_result(self, status: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            **status,
            "model_type": "logistic_regression",
            "label": self.config.label,
            "row_count": len(rows),
            "race_count": len({row.get("race_id") for row in rows}),
        }

    def _data_status(self, frame: pd.DataFrame) -> dict[str, Any]:
        if frame.empty or self.config.label not in frame:
            return {"status": "insufficient_data", "reason": "no_rows"}
        frame = frame.dropna(subset=[self.config.label])
        if len(frame) < self.config.min_rows:
            return {"status": "insufficient_data", "reason": "too_few_rows", "required_rows": self.config.min_rows}
        race_count = int(frame["race_id"].nunique())
        if race_count < self.config.min_races:
            return {
                "status": "insufficient_data",
                "reason": "too_few_races",
                "required_races": self.config.min_races,
                "race_count": race_count,
            }
        positives = int(frame[self.config.label].sum())
        negatives = int(len(frame) - positives)
        if positives < self.config.min_positive or negatives < self.config.min_negative:
            return {
                "status": "insufficient_data",
                "reason": "class_imbalance",
                "required_positive": self.config.min_positive,
                "required_negative": self.config.min_negative,
                "positive_count": positives,
                "negative_count": negatives,
            }
        return {"status": "ok"}

    def _features(self, frame: pd.DataFrame) -> pd.DataFrame:
        feature_columns = [column for column in frame.columns if column in BASELINE_FEATURES or column.startswith("feature__")]
        if not feature_columns:
            return pd.DataFrame(index=frame.index)
        numeric: dict[str, pd.Series] = {}
        for column in feature_columns:
            if column in EXCLUDED_NUMERIC_FEATURES:
                continue
            if not self.config.include_jev and column.startswith("feature__jev_"):
                continue
            values = pd.to_numeric(frame[column], errors="coerce")
            if values.notna().any():
                numeric[column] = values.replace([math.inf, -math.inf], float("nan"))
        return pd.DataFrame(numeric, index=frame.index)

    def _new_model(self) -> Pipeline:
        return make_pipeline(
            SimpleImputer(strategy="median", add_indicator=True),
            StandardScaler(),
            LogisticRegression(max_iter=1000, class_weight="balanced", random_state=0),
        )

    def _race_holdout(self, frame: pd.DataFrame) -> dict[str, Any]:
        labeled = frame.dropna(subset=[self.config.label])
        race_ids = sorted(
            (race_id for race_id in labeled["race_id"].dropna().unique()),
            key=lambda race_id: self._race_sort_key(labeled, race_id),
        )
        if len(race_ids) < 2:
            return {"status": "unavailable", "reason": "too_few_races", "fold_count": 0}

        labels: list[int] = []
        rule_probabilities: list[float] = []
        model_probabilities: list[float] = []
        blended_probabilities: list[float] = []
        skipped: list[str] = []
        evaluated_races: list[str] = []
        rule_top1_hits = 0
        blended_top1_hits = 0
        rule_top3_hits = 0
        blended_top3_hits = 0
        for index, race_id in enumerate(race_ids):
            prior_race_ids = race_ids[:index]
            train_frame = labeled[labeled["race_id"].isin(prior_race_ids)]
            test_frame = labeled[labeled["race_id"] == race_id]
            y_train = train_frame[self.config.label].astype(int)
            positives = int(y_train.sum())
            negatives = int(len(y_train) - positives)
            if positives < self.config.min_positive or negatives < self.config.min_negative:
                skipped.append(str(race_id))
                continue

            X_train = self._features(train_frame)
            X_test = self._features(test_frame).reindex(columns=X_train.columns, fill_value=0.0)
            model = self._new_model()
            model.fit(X_train, y_train)
            fold_rule = self._normalize(test_frame["ai_win_probability"].fillna(0.0).astype(float).tolist())
            fold_model = [float(value) for value in model.predict_proba(X_test)[:, 1]]
            fold_blended = self.blend_probabilities(fold_rule, fold_model)
            fold_labels = test_frame[self.config.label].astype(int).tolist()
            rule_order = sorted(range(len(fold_rule)), key=lambda item: fold_rule[item], reverse=True)
            blended_order = sorted(range(len(fold_blended)), key=lambda item: fold_blended[item], reverse=True)
            rule_top1_hits += int(bool(rule_order) and fold_labels[rule_order[0]] == 1)
            blended_top1_hits += int(bool(blended_order) and fold_labels[blended_order[0]] == 1)
            rule_top3_hits += int(any(fold_labels[item] == 1 for item in rule_order[:3]))
            blended_top3_hits += int(any(fold_labels[item] == 1 for item in blended_order[:3]))
            labels.extend(fold_labels)
            rule_probabilities.extend(fold_rule)
            model_probabilities.extend(self._normalize(fold_model))
            blended_probabilities.extend(fold_blended)
            evaluated_races.append(str(race_id))

        if len(evaluated_races) < self.config.min_holdout_folds:
            return {
                "status": "unavailable",
                "reason": "too_few_walk_forward_folds",
                "fold_count": len(evaluated_races),
                "required_folds": self.config.min_holdout_folds,
                "skipped_races": skipped,
            }
        rule_brier = float(brier_score_loss(labels, rule_probabilities))
        blended_brier = float(brier_score_loss(labels, blended_probabilities))
        rule_log_loss = float(log_loss(labels, rule_probabilities, labels=[0, 1]))
        blended_log_loss = float(log_loss(labels, blended_probabilities, labels=[0, 1]))
        rule_calibration_error = self._expected_calibration_error(labels, rule_probabilities)
        model_calibration_error = self._expected_calibration_error(labels, model_probabilities)
        blended_calibration_error = self._expected_calibration_error(labels, blended_probabilities)
        brier_improvement = rule_brier - blended_brier
        fold_count = len(evaluated_races)
        rule_top1_accuracy = rule_top1_hits / fold_count
        blended_top1_accuracy = blended_top1_hits / fold_count
        rule_top3_hit_rate = rule_top3_hits / fold_count
        blended_top3_hit_rate = blended_top3_hits / fold_count
        blend_approved = self._blend_is_approved(
            rule_brier,
            blended_brier,
            rule_log_loss,
            blended_log_loss,
            rule_top1_accuracy,
            blended_top1_accuracy,
            rule_top3_hit_rate,
            blended_top3_hit_rate,
        )
        return {
            "status": "evaluated",
            "validation_scheme": "walk_forward",
            "fold_count": len(evaluated_races),
            "evaluated_races": evaluated_races,
            "skipped_race_count": len(skipped),
            "skipped_races": skipped,
            "sample_count": len(labels),
            "rule_brier_score": round(rule_brier, 6),
            "rule_log_loss": round(rule_log_loss, 6),
            "rule_calibration_error": round(rule_calibration_error, 6),
            "model_brier_score": round(float(brier_score_loss(labels, model_probabilities)), 6),
            "model_log_loss": round(float(log_loss(labels, model_probabilities, labels=[0, 1])), 6),
            "model_calibration_error": round(model_calibration_error, 6),
            "blended_brier_score": round(blended_brier, 6),
            "blended_log_loss": round(blended_log_loss, 6),
            "blended_calibration_error": round(blended_calibration_error, 6),
            "brier_improvement": round(brier_improvement, 6),
            "required_brier_improvement": self.config.min_brier_improvement,
            "rule_top1_accuracy": round(rule_top1_accuracy, 6),
            "blended_top1_accuracy": round(blended_top1_accuracy, 6),
            "rule_top3_hit_rate": round(rule_top3_hit_rate, 6),
            "blended_top3_hit_rate": round(blended_top3_hit_rate, 6),
            "blend_approved": blend_approved,
        }

    def _blend_is_approved(
        self,
        rule_brier: float,
        blended_brier: float,
        rule_log_loss: float,
        blended_log_loss: float,
        rule_top1_accuracy: float = 0.0,
        blended_top1_accuracy: float = 0.0,
        rule_top3_hit_rate: float = 0.0,
        blended_top3_hit_rate: float = 0.0,
    ) -> bool:
        return (
            rule_brier - blended_brier >= self.config.min_brier_improvement
            and blended_log_loss <= rule_log_loss
            and blended_top1_accuracy >= rule_top1_accuracy
            and blended_top3_hit_rate >= rule_top3_hit_rate
        )

    def _race_sort_key(self, frame: pd.DataFrame, race_id: Any) -> tuple[str, str]:
        race_rows = frame[frame["race_id"] == race_id]
        dates = race_rows["race_date"].dropna().astype(str)
        return (dates.iloc[0] if not dates.empty else str(race_id), str(race_id))

    def _expected_calibration_error(
        self,
        labels: list[int],
        probabilities: list[float],
        bins: int = 10,
    ) -> float:
        if not labels or len(labels) != len(probabilities):
            return 0.0
        error = 0.0
        total = len(labels)
        for index in range(bins):
            lower = index / bins
            upper = (index + 1) / bins
            members = [
                position
                for position, probability in enumerate(probabilities)
                if lower <= probability < upper or (index == bins - 1 and probability == upper)
            ]
            if not members:
                continue
            average_probability = sum(probabilities[position] for position in members) / len(members)
            observed_rate = sum(labels[position] for position in members) / len(members)
            error += len(members) / total * abs(average_probability - observed_rate)
        return error

    def _top_coefficients(self, columns, coefficients) -> dict[str, list[dict[str, Any]]]:
        rows = [
            {"feature": str(column), "coefficient": round(float(value), 6)}
            for column, value in zip(columns, coefficients, strict=True)
        ]
        return {
            "positive": sorted(rows, key=lambda row: row["coefficient"], reverse=True)[:10],
            "negative": sorted(rows, key=lambda row: row["coefficient"])[:10],
        }

    def _normalize(self, probabilities: list[float]) -> list[float]:
        values = [max(0.000001, float(value)) for value in probabilities]
        total = sum(values) or 1.0
        return [value / total for value in values]


def evaluate_challenger_dataset(dataset: dict[str, Any], include_jev: bool = True) -> dict[str, Any]:
    split = temporal_race_split(dataset.get("rows") or [])
    train_rows = list(split["train"])
    test_rows = list(split["test"])
    if not train_rows or not test_rows:
        return {"status": "unavailable", "reason": "too_few_temporal_races"}

    probabilities: dict[str, list[float]] = {}
    labels: dict[str, list[int]] = {}
    for target in ("actual_win", "actual_top3"):
        trainer = LogisticBaselineTrainer(TrainingConfig(label=target, include_jev=include_jev))
        result, model = trainer.fit({"rows": train_rows})
        if model is None:
            return {"status": "unavailable", "reason": result.get("reason"), "target": target}
        valid_test_rows = [row for row in test_rows if row.get(target) is not None]
        values = model.predict_rows(valid_test_rows)
        probabilities[target] = values
        labels[target] = [int(bool(row[target])) for row in valid_test_rows]

    win_rows = [row for row in test_rows if row.get("actual_win") is not None]
    win_probabilities = _normalize_probabilities_by_race(win_rows, probabilities["actual_win"])
    baseline_win_probabilities = _normalize_probabilities_by_race(
        win_rows, [float(row.get("ai_win_probability") or 0.0) for row in win_rows]
    )
    top3_rows = [row for row in test_rows if row.get("actual_top3") is not None]
    top3_probabilities = probabilities["actual_top3"]
    rank_metrics = _race_rank_metrics(win_rows, win_probabilities, top3_rows, top3_probabilities)
    baseline_top3_probabilities = [float(row.get("ai_top3_probability") or 0.0) for row in top3_rows]
    baseline_rank_metrics = _race_rank_metrics(
        win_rows,
        baseline_win_probabilities,
        top3_rows,
        baseline_top3_probabilities,
    )
    relative_error = _relative_finish_error(train_rows, test_rows, include_jev)
    jev_available_rows = sum(
        1
        for row in train_rows
        if any(
            key.startswith("jev_")
            and key not in {"jev_qualitative_adjustment", "jev_confidence", "jev_uncertainty"}
            and value is not None
            for key, value in (row.get("features") or {}).items()
        )
    )
    test_races = list(split["test_race_ids"])
    return {
        "status": "evaluated",
        "validation_scheme": "chronological_race_70_15_15",
        "include_jev": include_jev,
        "jev_available_rows": jev_available_rows,
        "training_start": _period_start(train_rows),
        "training_end": _period_end(train_rows),
        "evaluation_start": _period_start(test_rows),
        "evaluation_end": _period_end(test_rows),
        "race_count": len(test_races),
        "horse_count": len(test_rows),
        "brier_score": round(float(brier_score_loss(labels["actual_win"], win_probabilities)), 6),
        "log_loss": round(float(log_loss(labels["actual_win"], win_probabilities, labels=[0, 1])), 6),
        "calibration_error": round(
            LogisticBaselineTrainer()._expected_calibration_error(labels["actual_win"], win_probabilities), 6
        ),
        "top3_brier_score": round(float(brier_score_loss(labels["actual_top3"], top3_probabilities)), 6),
        "top3_recall": rank_metrics["top3_recall"],
        "winner_rank_mean": rank_metrics["winner_rank_mean"],
        "prediction_top3_mean_finish": rank_metrics["prediction_top3_mean_finish"],
        "relative_finish_error": relative_error,
        "rank_relative_finish_error": _rank_relative_finish_error(win_rows, win_probabilities),
        "baseline_brier_score": round(
            float(brier_score_loss(labels["actual_win"], baseline_win_probabilities)), 6
        ),
        "baseline_log_loss": round(
            float(log_loss(labels["actual_win"], baseline_win_probabilities, labels=[0, 1])), 6
        ),
        "baseline_calibration_error": round(
            LogisticBaselineTrainer()._expected_calibration_error(
                labels["actual_win"], baseline_win_probabilities
            ),
            6,
        ),
        "baseline_top3_recall": baseline_rank_metrics["top3_recall"],
        "baseline_winner_rank_mean": baseline_rank_metrics["winner_rank_mean"],
        "baseline_prediction_top3_mean_finish": baseline_rank_metrics["prediction_top3_mean_finish"],
        "baseline_rank_relative_finish_error": _rank_relative_finish_error(
            win_rows, baseline_win_probabilities
        ),
        "train_race_ids": list(split["train_race_ids"]),
        "validation_race_ids": list(split["validation_race_ids"]),
        "test_race_ids": test_races,
    }


def _normalize_probabilities_by_race(rows: list[dict[str, Any]], values: list[float]) -> list[float]:
    output = [0.0] * len(values)
    by_race: dict[str, list[int]] = {}
    for index, row in enumerate(rows):
        by_race.setdefault(str(row.get("race_id")), []).append(index)
    for positions in by_race.values():
        total = sum(max(0.000001, values[index]) for index in positions)
        for index in positions:
            output[index] = max(0.000001, values[index]) / total
    return output


def _race_rank_metrics(
    win_rows: list[dict[str, Any]],
    win_probabilities: list[float],
    top3_rows: list[dict[str, Any]],
    top3_probabilities: list[float],
) -> dict[str, float | None]:
    winner_ranks: list[int] = []
    predicted_finishes: list[float] = []
    actual_top3_count = 0
    recovered_top3 = 0
    for rows, values, mode in (
        (win_rows, win_probabilities, "win"),
        (top3_rows, top3_probabilities, "top3"),
    ):
        by_race: dict[str, list[int]] = {}
        for index, row in enumerate(rows):
            by_race.setdefault(str(row.get("race_id")), []).append(index)
        for positions in by_race.values():
            ordered = sorted(positions, key=lambda index: values[index], reverse=True)
            if mode == "win":
                winner_position = next((index for index in positions if bool(rows[index].get("actual_win"))), None)
                if winner_position is not None:
                    winner_ranks.append(ordered.index(winner_position) + 1)
                predicted_finishes.extend(
                    float(rows[index]["finish_position"])
                    for index in ordered[:3]
                    if rows[index].get("finish_position") is not None
                )
            else:
                actual = {index for index in positions if bool(rows[index].get("actual_top3"))}
                actual_top3_count += len(actual)
                recovered_top3 += len(actual.intersection(ordered[:3]))
    return {
        "top3_recall": round(recovered_top3 / actual_top3_count, 6) if actual_top3_count else None,
        "winner_rank_mean": round(sum(winner_ranks) / len(winner_ranks), 6) if winner_ranks else None,
        "prediction_top3_mean_finish": (
            round(sum(predicted_finishes) / len(predicted_finishes), 6) if predicted_finishes else None
        ),
    }


def _relative_finish_error(train_rows: list[dict[str, Any]], test_rows: list[dict[str, Any]], include_jev: bool) -> float | None:
    train = [row for row in train_rows if row.get("relative_finish") is not None]
    test = [row for row in test_rows if row.get("relative_finish") is not None]
    if len(train) < 20 or not test:
        return None
    trainer = LogisticBaselineTrainer(TrainingConfig(include_jev=include_jev))
    train_frame = trainer._frame(train)
    test_frame = trainer._frame(test)
    X_train = trainer._features(train_frame)
    X_test = trainer._features(test_frame).reindex(columns=X_train.columns)
    model = make_pipeline(SimpleImputer(strategy="median", add_indicator=True), StandardScaler(), Ridge(alpha=5.0))
    model.fit(X_train, [float(row["relative_finish"]) for row in train])
    predicted = [max(0.0, min(1.0, float(value))) for value in model.predict(X_test)]
    return round(float(mean_absolute_error([float(row["relative_finish"]) for row in test], predicted)), 6)


def _rank_relative_finish_error(rows: list[dict[str, Any]], values: list[float]) -> float | None:
    errors: list[float] = []
    by_race: dict[str, list[int]] = {}
    for index, row in enumerate(rows):
        if row.get("relative_finish") is not None:
            by_race.setdefault(str(row.get("race_id")), []).append(index)
    for positions in by_race.values():
        if len(positions) <= 1:
            continue
        ordered = sorted(positions, key=lambda index: values[index], reverse=True)
        for rank, index in enumerate(ordered, start=1):
            predicted_relative = 1.0 - ((rank - 1) / (len(positions) - 1))
            errors.append(abs(float(rows[index]["relative_finish"]) - predicted_relative))
    return round(sum(errors) / len(errors), 6) if errors else None


def _period_start(rows: list[dict[str, Any]]) -> str | None:
    dates = sorted(str(row["race_date"]) for row in rows if row.get("race_date"))
    return dates[0] if dates else None


def _period_end(rows: list[dict[str, Any]]) -> str | None:
    dates = sorted(str(row["race_date"]) for row in rows if row.get("race_date"))
    return dates[-1] if dates else None
