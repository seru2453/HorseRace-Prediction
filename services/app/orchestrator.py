from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from database.repository import RaceRepository
from database.session import make_session_factory
from services.common.models import AnalysisResult, HorseFeature, PredictionData
from services.common.race_eligibility import is_supported_flat_race, race_start_datetime
from services.data_provider.base import RacingDataProvider
from services.data_provider.mock import MockProvider
from services.features.generator import FeatureGenerator
from services.leakage import assert_no_future_features
from services.ml.baseline import (
    LOGISTIC_BASELINE_VERSION,
    LogisticBaselineTrainer,
    TrainingConfig,
    prediction_rows_from_current,
)
from services.prediction.expected_value import estimate_expected_value
from services.prediction.jev import JevQualitativeEvaluator
from services.prediction.rule_based import RuleBasedPredictor
from services.simulation.monte_carlo import MonteCarloSimulator


@dataclass(frozen=True)
class AnalysisRequest:
    race_date: str
    racecourse: str
    race_number: int
    iterations: int = 10_000
    seed: int = 20260816
    save: bool = True
    enforce_pre_race: bool = False
    execution_time: datetime | None = None


class RaceAnalysisService:
    def __init__(self, provider: RacingDataProvider | None = None, database_url: str | None = None) -> None:
        self.provider = provider or MockProvider()
        self.session_factory = make_session_factory(database_url)
        self.feature_generator = FeatureGenerator()
        self.predictor = RuleBasedPredictor()
        self.qualitative_evaluator = JevQualitativeEvaluator()
        self.simulator = MonteCarloSimulator()

    def analyze(self, request: AnalysisRequest) -> AnalysisResult:
        race = self.provider.get_race(request.race_date, request.racecourse, request.race_number)
        if request.enforce_pre_race:
            self._validate_prediction_time(race, request.execution_time)
        entries = self.provider.get_race_entries(race.race_id)
        histories = {entry.horse.horse_id: self.provider.get_horse_history(entry.horse) for entry in entries}
        factor_adjustments: dict[str, float] = {}
        training_dataset: dict = {}
        pedigree_stats: dict[str, dict] = {}
        track_bias: dict = {}
        qualitative_evidence = {}
        with self.session_factory() as session:
            repo = RaceRepository(session)
            for entry in entries:
                if not histories.get(entry.horse.horse_id):
                    histories[entry.horse.horse_id] = repo.get_horse_history(
                        entry.horse.horse_id,
                        before_date=str(race.race_date.value),
                    )
            factor_adjustments = repo.active_factor_adjustments()
            training_dataset = repo.training_dataset()
            pedigree_stats = repo.pedigree_condition_stats(race, entries)
            track_bias = repo.track_bias_stats(race)
            qualitative_evidence = repo.qualitative_evidence(
                race.race_id,
                request.execution_time or datetime.now(timezone.utc),
            )
        from services.parser.race_parser import RaceParser

        parse_quality = RaceParser().validate(race, entries)
        features = self.feature_generator.generate(
            race,
            entries,
            histories,
            pedigree_stats=pedigree_stats,
            track_bias=track_bias,
        )
        features = self.qualitative_evaluator.apply(race, entries, features, qualitative_evidence)
        assert_no_future_features([item.features for item in features])
        predictions = RuleBasedPredictor(factor_adjustments=factor_adjustments).predict(features)
        challenger_predictions, ml_training_result = self._run_logistic_challenger(
            race.race_id, features, predictions, training_dataset
        )
        predictions = self._apply_expected_values(features, predictions)
        simulation = self.simulator.simulate(race.race_id, predictions, request.iterations, request.seed)
        result = AnalysisResult(
            race=race,
            entries=entries,
            parse_quality=parse_quality,
            predictions=predictions,
            simulation=simulation,
            message=self._analysis_message(ml_training_result),
        )
        if request.save:
            with self.session_factory() as session:
                repo = RaceRepository(session)
                repo.save_analysis(result)
                repo.save_analysis_run(result, type(self.provider).__name__, features)
                snapshot_ids = repo.save_prediction_snapshots(result, features, request.execution_time)
                repo.save_challenger_predictions(snapshot_ids, challenger_predictions)
                repo.save_past_performances(race.race_id, histories)
        return result

    def _validate_prediction_time(self, race, execution_time: datetime | None = None) -> None:
        if not is_supported_flat_race(race.surface.value, race.race_class.value):
            raise ValueError("障害レースは平地用予測モデルの対象外です。")
        start = race_start_datetime(race.race_date.value, race.start_time.value)
        if start is None:
            raise ValueError("発走時刻を確認できないため予測を保存できません。")
        current = execution_time or datetime.now(ZoneInfo("Asia/Tokyo"))
        if current.tzinfo is None:
            current = current.replace(tzinfo=ZoneInfo("Asia/Tokyo"))
        if current.astimezone(ZoneInfo("Asia/Tokyo")) >= start:
            raise ValueError("発走後のレースは予測を作成・保存できません。")

    def _run_logistic_challenger(
        self,
        race_id: str,
        features: list[HorseFeature],
        predictions: list[PredictionData],
        training_dataset: dict,
    ) -> tuple[list[dict], dict]:
        dataset = self._exclude_race_from_training_dataset(training_dataset, race_id)
        current_rows = prediction_rows_from_current(features, predictions)
        shadow_rows: list[dict] = []
        results: dict[str, dict] = {}
        for target in ("actual_win", "actual_top3"):
            trainer = LogisticBaselineTrainer(TrainingConfig(label=target))
            training_result, model = trainer.fit(dataset)
            results[target] = training_result
            if model is None:
                continue
            values = model.predict_rows(current_rows)
            if target == "actual_win":
                values = trainer._normalize(values)
            for row, value in zip(current_rows, values, strict=True):
                shadow_rows.append(
                    {
                        "horse_id": row["horse_id"],
                        "model_version": LOGISTIC_BASELINE_VERSION,
                        "feature_version": predictions[0].feature_version if predictions else None,
                        "target": target,
                        "predicted_value": max(0.0, min(1.0, float(value))),
                        "metadata": {
                            "mode": "shadow",
                            "training_rows": training_result.get("row_count", 0),
                            "training_races": training_result.get("race_count", 0),
                        },
                    }
                )
        return shadow_rows, {
            "status": "shadow_generated" if shadow_rows else "insufficient_data",
            "application_status": "shadow_only",
            "model_version": LOGISTIC_BASELINE_VERSION,
            "targets": results,
            "prediction_count": len(shadow_rows),
        }

    def _apply_expected_values(
        self,
        features: list[HorseFeature],
        predictions: list[PredictionData],
    ) -> list[PredictionData]:
        features_by_horse = {feature.horse_id: feature.features for feature in features}
        valued_predictions: list[PredictionData] = []
        for prediction in predictions:
            horse_features = features_by_horse.get(prediction.horse_id, {})
            estimate = estimate_expected_value(
                prediction.win_probability,
                horse_features.get("win_odds"),
                prediction.confidence,
                horse_features.get("field_implied_win_probability_share"),
            )
            valued_predictions.append(prediction.model_copy(update=estimate) if estimate else prediction)
        return valued_predictions

    def _exclude_race_from_training_dataset(self, dataset: dict, race_id: str) -> dict:
        rows = [row for row in dataset.get("rows", []) if row.get("race_id") != race_id]
        return {
            **dataset,
            "rows": rows,
            "row_count": len(rows),
            "race_count": len({row.get("race_id") for row in rows}),
        }

    def _analysis_message(self, training_result: dict) -> str:
        if training_result.get("application_status") == "shadow_only":
            return "Championはbaseline-v1です。logistic-v1は比較用shadowとして保存しています。"
        reason = training_result.get("reason")
        if reason:
            return f"現在は学習データ不足のため簡易モデルを使用しています。学習補助: {reason}"
        return "現在はデータ不足のため簡易モデルを使用しています。"
