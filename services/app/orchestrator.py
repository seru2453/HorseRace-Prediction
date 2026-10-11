from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from time import perf_counter
from zoneinfo import ZoneInfo

from database.repository import RaceRepository
from database.session import get_session_factory
from services.common.models import AnalysisResult, HorseFeature, PredictionData
from services.common.race_eligibility import is_supported_flat_race, race_start_datetime
from services.data_provider.base import RacingDataProvider
from services.data_provider.mock import MockProvider
from services.features.generator import FeatureGenerator
from services.leakage import assert_no_future_features
from services.ml.artifacts import load_logistic_artifact
from services.ml.baseline import prediction_rows_from_current
from services.prediction.expected_value import estimate_expected_value
from services.prediction.jev import JevQualitativeEvaluator
from services.prediction.rule_based import RuleBasedPredictor
from services.simulation.monte_carlo import MonteCarloSimulator

logger = logging.getLogger(__name__)


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
    def __init__(
        self,
        provider: RacingDataProvider | None = None,
        database_url: str | None = None,
    ) -> None:
        self.provider = provider or MockProvider()
        setup_started = perf_counter()
        self.session_factory = get_session_factory(database_url)
        logger.info("analysis_timing_ms session_factory_init=%.1f", (perf_counter() - setup_started) * 1000)
        self.feature_generator = FeatureGenerator()
        self.predictor = RuleBasedPredictor()
        self.qualitative_evaluator = JevQualitativeEvaluator()
        self.simulator = MonteCarloSimulator()

    def analyze(self, request: AnalysisRequest) -> AnalysisResult:
        timings: dict[str, float] = {}
        started = perf_counter()
        try:
            return self._analyze(request, timings)
        finally:
            timings["total"] = (perf_counter() - started) * 1000
            logger.info("analysis_timing_ms %s", " ".join(f"{name}={value:.1f}" for name, value in sorted(timings.items())))

    def _analyze(self, request: AnalysisRequest, timings: dict[str, float]) -> AnalysisResult:
        def timed(name, action):
            started = perf_counter()
            try:
                return action()
            finally:
                timings[name] = (perf_counter() - started) * 1000

        def fetch_race_data():
            race = self.provider.get_race(request.race_date, request.racecourse, request.race_number)
            if request.enforce_pre_race:
                self._validate_prediction_time(race, request.execution_time)
            entries = self.provider.get_race_entries(race.race_id)
            histories = {entry.horse.horse_id: self.provider.get_horse_history(entry.horse) for entry in entries}
            return race, entries, histories

        race, entries, histories = timed("provider_fetch", fetch_race_data)
        factor_adjustments: dict[str, float] = {}
        aptitude_histories: dict[str, list[dict]] = {}
        pedigree_stats: dict[str, dict] = {}
        track_bias: dict = {}
        qualitative_evidence = {}
        with self.session_factory() as session:
            repo = RaceRepository(session)

            def load_history_profiles():
                horse_ids = [entry.horse.horse_id for entry in entries]
                stored_histories = repo.get_horse_histories(
                    horse_ids,
                    limit=20,
                    before_date=str(race.race_date.value),
                )
                for horse_id in horse_ids:
                    provider_history = histories.get(horse_id) or []
                    stored_history = stored_histories.get(horse_id) or []
                    combined: dict[tuple, dict] = {}
                    for item in stored_history + provider_history:
                        item_date = str(item.get("race_date") or "")
                        if item_date and item_date >= str(race.race_date.value):
                            continue
                        key = (
                            item.get("race_date"),
                            item.get("racecourse"),
                            item.get("race_name"),
                            item.get("distance"),
                        )
                        combined[key] = {**combined.get(key, {}), **item}
                    merged = sorted(
                        combined.values(),
                        key=lambda item: str(item.get("race_date") or ""),
                        reverse=True,
                    )[:20]
                    aptitude_histories[horse_id] = merged
                    if not provider_history:
                        histories[horse_id] = merged[:5]

            timed("history_profiles", load_history_profiles)
            factor_adjustments = timed("factor_adjustments", repo.active_factor_adjustments)
            pedigree_stats = timed("pedigree_stats", lambda: repo.pedigree_condition_stats(race, entries))
            track_bias = timed("track_bias", lambda: repo.track_bias_stats(race))
            qualitative_evidence = timed(
                "qualitative_evidence",
                lambda: repo.qualitative_evidence(
                    race.race_id,
                    request.execution_time or datetime.now(timezone.utc),
                ),
            )
        from services.parser.race_parser import RaceParser

        parse_quality = timed("parse_validation", lambda: RaceParser().validate(race, entries))
        features = timed(
            "feature_generation",
            lambda: self.feature_generator.generate(
                race,
                entries,
                histories,
                aptitude_histories=aptitude_histories,
                pedigree_stats=pedigree_stats,
                track_bias=track_bias,
            ),
        )
        features = timed("jev", lambda: self.qualitative_evaluator.apply(race, entries, features, qualitative_evidence))
        assert_no_future_features([item.features for item in features])
        predictions = timed(
            "rule_based",
            lambda: RuleBasedPredictor(factor_adjustments=factor_adjustments).predict(features),
        )
        challenger_predictions, model_status = timed(
            "logistic_predict",
            lambda: self._predict_with_saved_logistic_models(features, predictions),
        )
        predictions = timed("expected_value", lambda: self._apply_expected_values(features, predictions))
        simulation = timed(
            "monte_carlo",
            lambda: self.simulator.simulate(race.race_id, predictions, request.iterations, request.seed),
        )
        result = AnalysisResult(
            race=race,
            entries=entries,
            parse_quality=parse_quality,
            predictions=predictions,
            simulation=simulation,
            message=self._analysis_message(model_status),
        )
        if request.save:
            def save_analysis():
                with self.session_factory() as session:
                    repo = RaceRepository(session)
                    repo.save_analysis(result)
                    repo.save_analysis_run(result, type(self.provider).__name__, features)
                    snapshot_ids = repo.save_prediction_snapshots(result, features, request.execution_time)
                    repo.save_challenger_predictions(snapshot_ids, challenger_predictions)
                    repo.save_past_performances(race.race_id, histories)

            timed("save", save_analysis)
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

    def _predict_with_saved_logistic_models(
        self,
        features: list[HorseFeature],
        predictions: list[PredictionData],
    ) -> tuple[list[dict], dict]:
        current_rows = prediction_rows_from_current(features, predictions)
        shadow_rows: list[dict] = []
        targets: dict[str, dict] = {}
        for target in ("actual_win", "actual_top3"):
            artifact = load_logistic_artifact(target)
            if artifact is None:
                targets[target] = {"status": "unavailable", "reason": "no_saved_model"}
                continue
            try:
                values = artifact["model"].predict_rows(current_rows)
            except Exception:
                logger.exception("saved_logistic_prediction_failed label=%s; continuing with RuleBased only", target)
                targets[target] = {"status": "unavailable", "reason": "prediction_failed"}
                continue
            if target == "actual_win":
                values = self._normalize_probabilities(values)
            metrics = artifact.get("metrics") or {}
            targets[target] = {
                "status": "predicted",
                "trained_at": artifact.get("trained_at"),
                "row_count": metrics.get("row_count", 0),
            }
            for row, value in zip(current_rows, values, strict=True):
                shadow_rows.append(
                    {
                        "horse_id": row["horse_id"],
                        "model_version": artifact["model_version"],
                        "feature_version": predictions[0].feature_version if predictions else None,
                        "target": target,
                        "predicted_value": max(0.0, min(1.0, float(value))),
                        "metadata": {
                            "mode": "shadow",
                            "training_rows": metrics.get("row_count", 0),
                            "training_races": metrics.get("race_count", 0),
                            "trained_at": artifact.get("trained_at"),
                        },
                    }
                )
        return shadow_rows, {
            "status": "shadow_generated" if shadow_rows else "no_saved_model",
            "application_status": "shadow_only",
            "targets": targets,
            "prediction_count": len(shadow_rows),
        }

    @staticmethod
    def _normalize_probabilities(probabilities: list[float]) -> list[float]:
        values = [max(0.000001, float(value)) for value in probabilities]
        total = sum(values) or 1.0
        return [value / total for value in values]

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

    def _analysis_message(self, training_result: dict) -> str:
        if training_result.get("status") == "shadow_generated" and training_result.get("application_status") == "shadow_only":
            return "Championはbaseline-v1です。logistic-v1は比較用shadowとして保存しています。"
        reason = training_result.get("reason")
        if reason:
            return f"現在は学習データ不足のため簡易モデルを使用しています。学習補助: {reason}"
        return "RuleBased予測を使用しています。比較用モデルは学習後にmake trainで更新できます。"
