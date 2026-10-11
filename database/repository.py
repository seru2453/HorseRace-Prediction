from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session
from statistics import mean

from database.models import (
    AnalysisRun,
    ChallengerPrediction,
    Horse,
    ModelEvaluation,
    PastPerformance,
    Prediction,
    PredictionMetric,
    PredictionReview,
    PredictionSnapshot,
    QualitativeEvidence,
    Race,
    RaceEntry,
    RaceResult,
    RaceResultEntry,
    Simulation,
    SourceReliability,
    TrainingRun,
    UserPrediction,
)
from services.evidence.collector import PredictionEvidence
from services.ml.targets import relative_finish, relative_finish_category
from services.common.models import (
    AnalysisResult,
    HorseFeature,
    PredictionData,
    RaceData,
    RaceEntryData,
    RaceResultData,
    UserPredictionInput,
)
from services.common.race_eligibility import is_supported_flat_race, prediction_lead_minutes
from services.prediction.config import DEFAULT_STABILITY_CONFIG


class RaceRepository:
    EXPORT_FORMAT_VERSION = "horseprediction-export-v1"

    def __init__(self, session: Session) -> None:
        self.session = session

    def save_analysis(self, analysis: AnalysisResult) -> None:
        race = self.session.get(Race, analysis.race.race_id) or Race(
            race_id=analysis.race.race_id,
            race_date=str(analysis.race.race_date.value),
            racecourse=str(analysis.race.racecourse.value),
            race_number=int(analysis.race.race_number.value),
            payload=analysis.race.model_dump(mode="json"),
        )
        race.payload = analysis.race.model_dump(mode="json")
        self.session.add(race)

        for entry in analysis.entries:
            horse = self.session.get(Horse, entry.horse.horse_id) or Horse(
                horse_id=entry.horse.horse_id,
                horse_name=str(entry.horse.horse_name.value),
                payload=entry.horse.model_dump(mode="json"),
            )
            horse.horse_name = str(entry.horse.horse_name.value)
            horse.payload = entry.horse.model_dump(mode="json")
            self.session.add(horse)
            existing = self.session.scalar(
                select(RaceEntry).where(RaceEntry.race_id == entry.race_id, RaceEntry.horse_number == int(entry.horse_number.value))
            )
            if existing is None:
                existing = RaceEntry(race_id=entry.race_id, horse_id=entry.horse.horse_id, horse_number=int(entry.horse_number.value))
            existing.frame_number = entry.frame_number.value
            existing.scratched = bool(entry.scratched.value)
            existing.payload = entry.model_dump(mode="json")
            self.session.add(existing)

        for prediction in analysis.predictions:
            self.session.add(
                Prediction(
                    race_id=prediction.race_id,
                    horse_id=prediction.horse_id,
                    model_version=prediction.model_version,
                    feature_version=prediction.feature_version,
                    win_probability=prediction.win_probability,
                    payload=prediction.model_dump(mode="json"),
                )
            )

        self.session.add(
            Simulation(
                race_id=analysis.simulation.race_id,
                iterations=analysis.simulation.iterations,
                seed=analysis.simulation.seed,
                payload=analysis.simulation.model_dump(mode="json"),
            )
        )
        self.session.commit()

    def pedigree_condition_stats(self, race: RaceData, entries: list[RaceEntryData]) -> dict[str, dict]:
        target_date = str(race.race_date.value)
        target_surface = str(race.surface.value or "")
        target_band = self._distance_band(race.distance.value)
        buckets: dict[tuple[str, str], list[bool]] = {}
        historical_races = self.session.scalars(
            select(Race).where(Race.race_date < target_date).order_by(Race.race_date)
        ).all()
        for historical_race in historical_races:
            result = self.session.get(RaceResult, historical_race.race_id)
            if result is None:
                continue
            surface = str(self._field_value(historical_race.payload or {}, "surface") or "")
            distance = self._field_value(historical_race.payload or {}, "distance")
            if surface != target_surface or self._distance_band(distance) != target_band:
                continue
            result_by_number = {
                int(item["horse_number"]): item
                for item in (result.payload or {}).get("entries", [])
                if item.get("horse_number") is not None
            }
            race_entries = self.session.scalars(
                select(RaceEntry).where(RaceEntry.race_id == historical_race.race_id)
            ).all()
            for historical_entry in race_entries:
                result_entry = result_by_number.get(historical_entry.horse_number)
                finish = result_entry.get("finish_position") if result_entry else None
                if finish is None:
                    continue
                horse_payload = (historical_entry.payload or {}).get("horse") or {}
                for relation in ("sire", "damsire"):
                    name = self._field_value(horse_payload, relation)
                    if name:
                        buckets.setdefault((relation, str(name)), []).append(int(finish) <= 3)

        stats: dict[str, dict] = {}
        for entry in entries:
            row: dict = {}
            for relation in ("sire", "damsire"):
                name = getattr(entry.horse, relation).value
                samples = buckets.get((relation, str(name)), []) if name else []
                row[f"{relation}_name"] = name
                row[f"{relation}_condition_starts"] = len(samples)
                row[f"{relation}_condition_top3_rate"] = (
                    sum(1 for value in samples if value) / len(samples) if samples else None
                )
            stats[entry.horse.horse_id] = row
        return stats

    def track_bias_stats(self, race: RaceData) -> dict:
        target_date = str(race.race_date.value)
        target_course = str(race.racecourse.value)
        target_number = int(race.race_number.value)
        target_surface = str(race.surface.value or "")
        target_band = self._distance_band(race.distance.value)
        candidates = self.session.scalars(
            select(Race)
            .where(Race.racecourse == target_course, Race.race_date <= target_date)
            .order_by(Race.race_date.desc(), Race.race_number.desc())
        ).all()
        weighted_positions: list[tuple[float, float]] = []
        same_day_index = 0
        race_samples = 0
        for historical_race in candidates:
            if historical_race.race_id == race.race_id:
                continue
            if historical_race.race_date == target_date and historical_race.race_number >= target_number:
                continue
            if historical_race.race_date < target_date:
                age_days = (
                    datetime.fromisoformat(target_date) - datetime.fromisoformat(historical_race.race_date)
                ).days
                if age_days > 1:
                    break
            surface = str(self._field_value(historical_race.payload or {}, "surface") or "")
            distance = self._field_value(historical_race.payload or {}, "distance")
            if surface != target_surface or self._distance_band(distance) != target_band:
                continue
            result = self.session.get(RaceResult, historical_race.race_id)
            if result is None:
                continue
            if historical_race.race_date == target_date:
                decay = DEFAULT_STABILITY_CONFIG.track_bias_decay[
                    min(same_day_index, len(DEFAULT_STABILITY_CONFIG.track_bias_decay) - 2)
                ]
                same_day_index += 1
            else:
                decay = DEFAULT_STABILITY_CONFIG.track_bias_decay[-1]
            entries_by_number = {
                row.horse_number: row
                for row in self.session.scalars(
                    select(RaceEntry).where(RaceEntry.race_id == historical_race.race_id)
                ).all()
            }
            used = False
            for result_entry in (result.payload or {}).get("entries", []):
                finish = result_entry.get("finish_position")
                if finish is None or int(finish) > 3:
                    continue
                entry = entries_by_number.get(int(result_entry["horse_number"]))
                frame = entry.frame_number if entry else None
                if frame is None:
                    continue
                weighted_positions.append(((int(frame) - 1) / 7.0, decay))
                used = True
            if used:
                race_samples += 1
            if same_day_index >= 4 and historical_race.race_date < target_date:
                break
        if not weighted_positions:
            return {"raw_bias": None, "sample_count": 0, "race_count": 0, "sample_status": "UNKNOWN"}
        total_weight = sum(weight for _, weight in weighted_positions)
        average_frame_ratio = sum(value * weight for value, weight in weighted_positions) / total_weight
        raw_bias = max(-0.03, min(0.03, (0.5 - average_frame_ratio) * 0.06))
        return {
            "raw_bias": round(raw_bias, 6),
            "sample_count": len(weighted_positions),
            "race_count": race_samples,
            "sample_status": self._sample_status(race_samples),
        }

    def _sample_status(self, count: int) -> str:
        if count <= 0:
            return "UNKNOWN"
        if count < DEFAULT_STABILITY_CONFIG.minimum_sample_size:
            return "LOW_SAMPLE"
        return "SUFFICIENT"

    def _distance_band(self, distance: object) -> str | None:
        if distance in (None, ""):
            return None
        meters = int(distance)
        if meters <= 1400:
            return "sprint"
        if meters <= 1800:
            return "mile"
        if meters <= 2200:
            return "middle"
        return "stayer"

    def save_prediction_snapshots(
        self,
        analysis: AnalysisResult,
        features: list[HorseFeature],
        prediction_timestamp: datetime | None = None,
    ) -> dict[str, int]:
        features_by_horse = {item.horse_id: item for item in features}
        stored_timestamp = prediction_timestamp
        if stored_timestamp is not None:
            if stored_timestamp.tzinfo is None:
                stored_timestamp = stored_timestamp.replace(tzinfo=timezone.utc)
            stored_timestamp = stored_timestamp.astimezone(timezone.utc)
        snapshot_ids: dict[str, int] = {}
        for prediction in analysis.predictions:
            feature = features_by_horse.get(prediction.horse_id)
            snapshot = PredictionSnapshot(
                race_id=prediction.race_id,
                horse_id=prediction.horse_id,
                horse_number=prediction.horse_number,
                prediction_timestamp=stored_timestamp or datetime.now(timezone.utc),
                model_version=prediction.model_version,
                feature_version=prediction.feature_version,
                simulation_seed=analysis.simulation.seed,
                ai_win_probability=prediction.win_probability,
                ai_top2_probability=prediction.top2_probability,
                ai_top3_probability=prediction.top3_probability,
                raw_features=feature.features if feature else {},
                prediction_payload=prediction.model_dump(mode="json"),
            )
            self.session.add(snapshot)
            self.session.flush()
            snapshot_ids[prediction.horse_id] = snapshot.id
            self._save_prediction_metric(snapshot, prediction.model_dump(mode="json"))
        self.session.commit()
        return snapshot_ids

    def _save_prediction_metric(self, snapshot: PredictionSnapshot, payload: dict) -> None:
        metric = self.session.get(PredictionMetric, snapshot.id) or PredictionMetric(
            prediction_snapshot_id=snapshot.id
        )
        metric.base_score = payload.get("base_score")
        metric.condition_adjustment = payload.get("condition_adjustment")
        metric.uncertainty = payload.get("uncertainty")
        metric.confidence = payload.get("confidence")
        metric.base_rank = payload.get("base_rank")
        metric.final_rank = payload.get("final_rank")
        metric.rank_delta = payload.get("rank_delta")
        metric.current_form = payload.get("current_form")
        metric.condition_fit = payload.get("condition_fit")
        metric.previous_race_residual = payload.get("previous_race_residual")
        metric.rebound_score = payload.get("rebound_score")
        metric.field_size = payload.get("field_size")
        metric.jev_features = payload.get("jev_features") or None
        self.session.add(metric)

    def save_challenger_predictions(
        self,
        snapshot_ids: dict[str, int],
        predictions: list[dict],
    ) -> int:
        saved = 0
        for prediction in predictions:
            snapshot_id = snapshot_ids.get(str(prediction.get("horse_id")))
            if snapshot_id is None:
                continue
            row = self.session.scalar(
                select(ChallengerPrediction).where(
                    ChallengerPrediction.prediction_snapshot_id == snapshot_id,
                    ChallengerPrediction.model_version == prediction["model_version"],
                    ChallengerPrediction.target == prediction["target"],
                )
            )
            if row is None:
                row = ChallengerPrediction(
                    prediction_snapshot_id=snapshot_id,
                    model_version=prediction["model_version"],
                    target=prediction["target"],
                )
            row.feature_version = prediction.get("feature_version")
            row.predicted_value = float(prediction["predicted_value"])
            row.metadata_json = prediction.get("metadata") or {}
            self.session.add(row)
            saved += 1
        self.session.commit()
        return saved

    def save_qualitative_evidence(self, evidence: PredictionEvidence) -> QualitativeEvidence:
        row = self.session.scalar(
            select(QualitativeEvidence).where(
                QualitativeEvidence.content_hash == evidence.content_hash,
                QualitativeEvidence.race_id == evidence.race_id,
                QualitativeEvidence.horse_id == evidence.horse_id,
            )
        )
        if row is None:
            row = QualitativeEvidence(
                content_hash=evidence.content_hash,
                race_id=evidence.race_id,
                horse_id=evidence.horse_id,
            )
        row.source_type = evidence.source_type
        row.source_name = evidence.source_name
        row.source_url = evidence.source_url
        row.published_at = evidence.published_at
        row.author = evidence.author
        row.raw_text = evidence.raw_text
        row.normalized_text = evidence.normalized_text
        row.confidence = evidence.confidence
        self.session.add(row)
        self.session.commit()
        return row

    def qualitative_evidence(
        self,
        race_id: str,
        as_of: datetime,
    ) -> dict[str, list[PredictionEvidence]]:
        rows = self.session.scalars(
            select(QualitativeEvidence).where(
                QualitativeEvidence.race_id == race_id,
                or_(
                    QualitativeEvidence.published_at <= as_of,
                    (
                        QualitativeEvidence.published_at.is_(None)
                        & (QualitativeEvidence.collected_at <= as_of)
                    ),
                ),
            )
        ).all()
        result: dict[str, list[PredictionEvidence]] = {}
        for row in rows:
            result.setdefault(row.horse_id, []).append(
                PredictionEvidence(
                    source_type=row.source_type,
                    source_name=row.source_name,
                    source_url=row.source_url,
                    published_at=row.published_at,
                    author=row.author,
                    race_id=row.race_id,
                    horse_id=row.horse_id,
                    raw_text=row.raw_text,
                    normalized_text=row.normalized_text,
                    confidence=row.confidence,
                    content_hash=row.content_hash,
                )
            )
        return result

    def save_analysis_run(self, analysis: AnalysisResult, provider_name: str, features: list[HorseFeature] | None = None) -> None:
        parse_quality = analysis.parse_quality
        feature_quality = self._feature_quality(features or [])
        self.session.add(
            AnalysisRun(
                race_id=analysis.race.race_id,
                provider_name=provider_name,
                parse_status=parse_quality.status,
                recognized_entries=parse_quality.recognized_entries,
                expected_entries=parse_quality.expected_entries,
                issue_count=len(parse_quality.issues),
                payload={
                    "provider_name": provider_name,
                    "parse_quality": parse_quality.model_dump(mode="json"),
                    "feature_quality": feature_quality,
                    "message": analysis.message,
                },
            )
        )
        self.session.commit()

    def _feature_quality(self, features: list[HorseFeature]) -> dict:
        unavailable_counts: dict[str, int] = {}
        total_values = 0
        available_values = 0
        for item in features:
            for key in item.unavailable_features:
                unavailable_counts[key] = unavailable_counts.get(key, 0) + 1
            for value in item.features.values():
                total_values += 1
                if value is not None:
                    available_values += 1
        return {
            "horse_count": len(features),
            "unavailable_counts": unavailable_counts,
            "missing_history_count": unavailable_counts.get("past_5_average_finish", 0),
            "available_feature_ratio": round(available_values / total_values, 6) if total_values else None,
        }

    def prediction_snapshots(self, race_id: str, limit: int = 50) -> list[dict]:
        rows = self.session.scalars(
            select(PredictionSnapshot)
            .where(PredictionSnapshot.race_id == race_id)
            .order_by(PredictionSnapshot.id.desc())
            .limit(limit)
        ).all()
        return [
            {
                "id": row.id,
                "race_id": row.race_id,
                "horse_id": row.horse_id,
                "horse_number": row.horse_number,
                "prediction_timestamp": row.prediction_timestamp.isoformat() if row.prediction_timestamp else None,
                "model_version": row.model_version,
                "feature_version": row.feature_version,
                "simulation_seed": row.simulation_seed,
                "ai_win_probability": row.ai_win_probability,
                "ai_top2_probability": row.ai_top2_probability,
                "ai_top3_probability": row.ai_top3_probability,
                "raw_features": row.raw_features,
                "prediction": row.prediction_payload,
            }
            for row in rows
        ]

    def race_history(
        self,
        limit: int = 20,
        racecourse: str | None = None,
        has_result: bool | None = None,
        has_review: bool | None = None,
    ) -> list[dict]:
        statement = select(Race).order_by(Race.created_at.desc())
        if racecourse:
            statement = statement.where(Race.racecourse == racecourse)
        races = self.session.scalars(statement).all()
        history = []
        for race in races:
            entries = self.session.scalars(select(RaceEntry).where(RaceEntry.race_id == race.race_id)).all()
            snapshots = self.session.scalars(
                select(PredictionSnapshot).where(PredictionSnapshot.race_id == race.race_id)
            ).all()
            latest_review = self.session.scalar(
                select(PredictionReview)
                .where(PredictionReview.race_id == race.race_id)
                .order_by(PredictionReview.created_at.desc(), PredictionReview.id.desc())
            )
            result = self.session.get(RaceResult, race.race_id)
            if has_result is not None and (result is not None) != has_result:
                continue
            if has_review is not None and (latest_review is not None) != has_review:
                continue
            latest_analysis_run = self._latest_analysis_run(race.race_id)
            latest_snapshot = max(snapshots, key=lambda item: item.id) if snapshots else None
            feature_quality = (latest_analysis_run.payload or {}).get("feature_quality") if latest_analysis_run else {}
            history.append(
                {
                    "race_id": race.race_id,
                    "race_date": race.race_date,
                    "racecourse": race.racecourse,
                    "race_number": race.race_number,
                    "race_name": self._field_value(race.payload or {}, "race_name"),
                    "start_time": self._field_value(race.payload or {}, "start_time"),
                    "surface": self._field_value(race.payload or {}, "surface"),
                    "distance": self._field_value(race.payload or {}, "distance"),
                    "track_condition": self._field_value(race.payload or {}, "track_condition"),
                    "entry_count": len(entries),
                    "snapshot_count": len(snapshots),
                    "has_result": result is not None,
                    "result_source": self._result_source(result.payload if result else None),
                    "review_count": 1 if latest_review else 0,
                    "parse_status": latest_analysis_run.parse_status if latest_analysis_run else None,
                    "parse_issue_count": latest_analysis_run.issue_count if latest_analysis_run else None,
                    "provider_name": latest_analysis_run.provider_name if latest_analysis_run else None,
                    "missing_history_count": feature_quality.get("missing_history_count"),
                    "available_feature_ratio": feature_quality.get("available_feature_ratio"),
                    "latest_snapshot_at": latest_snapshot.prediction_timestamp.isoformat() if latest_snapshot else None,
                    "latest_review_at": latest_review.created_at.isoformat() if latest_review else None,
                    "latest_ai_brier_score": latest_review.payload.get("ai_brier_score") if latest_review else None,
                    "latest_user_brier_score": latest_review.payload.get("user_brier_score") if latest_review else None,
                }
            )
            if len(history) >= limit:
                break
        return history

    def race_detail(self, race_id: str) -> dict | None:
        race = self.session.get(Race, race_id)
        if race is None:
            return None
        entries = self.session.scalars(
            select(RaceEntry).where(RaceEntry.race_id == race_id).order_by(RaceEntry.horse_number)
        ).all()
        snapshots = self._latest_snapshot_rows(race_id, len(entries) or 50)
        result = self.session.get(RaceResult, race_id)
        latest_review = self.session.scalar(
            select(PredictionReview)
            .where(PredictionReview.race_id == race_id)
            .order_by(PredictionReview.created_at.desc(), PredictionReview.id.desc())
        )
        user_predictions = self.session.scalars(
            select(UserPrediction).where(UserPrediction.race_id == race_id).order_by(UserPrediction.prediction_timestamp.desc())
        ).all()
        latest_simulation = self.session.scalar(
            select(Simulation).where(Simulation.race_id == race_id).order_by(Simulation.created_at.desc())
        )
        latest_analysis_run = self._latest_analysis_run(race_id)
        latest_user_predictions = self.latest_user_predictions(race_id)
        return {
            "race": race.payload,
            "entries": [entry.payload for entry in entries],
            "latest_predictions": [row.prediction_payload for row in reversed(snapshots)],
            "latest_snapshots": [self._snapshot_payload(row) for row in reversed(snapshots)],
            "latest_simulation": latest_simulation.payload if latest_simulation else None,
            "latest_analysis_run": self._analysis_run_payload(latest_analysis_run) if latest_analysis_run else None,
            "result": result.payload if result else None,
            "result_source": self._result_source(result.payload if result else None),
            "latest_review": latest_review.payload if latest_review else None,
            "latest_review_at": latest_review.created_at.isoformat() if latest_review else None,
            "latest_user_predictions": {
                horse_number: prediction.model_dump(mode="json")
                for horse_number, prediction in latest_user_predictions.items()
            },
            "user_predictions": [
                {
                    "horse_number": row.horse_number,
                    "prediction_timestamp": row.prediction_timestamp.isoformat() if row.prediction_timestamp else None,
                    "payload": row.payload,
                }
                for row in user_predictions
            ],
        }

    def export_data(self) -> dict:
        races = self.session.scalars(select(Race).order_by(Race.created_at)).all()
        race_entries = self.session.scalars(select(RaceEntry).order_by(RaceEntry.race_id, RaceEntry.horse_number)).all()
        horses = self.session.scalars(select(Horse).order_by(Horse.horse_id)).all()
        analysis_runs = self.session.scalars(select(AnalysisRun).order_by(AnalysisRun.created_at)).all()
        prediction_snapshots = self.session.scalars(select(PredictionSnapshot).order_by(PredictionSnapshot.id)).all()
        simulations = self.session.scalars(select(Simulation).order_by(Simulation.created_at)).all()
        user_predictions = self.session.scalars(select(UserPrediction).order_by(UserPrediction.prediction_timestamp)).all()
        race_results = self.session.scalars(select(RaceResult).order_by(RaceResult.stored_at)).all()
        prediction_reviews = self.session.scalars(select(PredictionReview).order_by(PredictionReview.created_at)).all()
        past_performances = self.session.scalars(select(PastPerformance).order_by(PastPerformance.stored_at)).all()
        model_evaluations = self.session.scalars(select(ModelEvaluation).order_by(ModelEvaluation.evaluated_at)).all()
        training_runs = self.session.scalars(select(TrainingRun).order_by(TrainingRun.trained_at)).all()
        challenger_predictions = self.session.scalars(select(ChallengerPrediction).order_by(ChallengerPrediction.id)).all()
        qualitative_evidence = self.session.scalars(select(QualitativeEvidence).order_by(QualitativeEvidence.id)).all()
        source_reliability = self.session.scalars(select(SourceReliability).order_by(SourceReliability.id)).all()
        data = {
            "races": [
                {
                    "race_id": row.race_id,
                    "race_date": row.race_date,
                    "racecourse": row.racecourse,
                    "race_number": row.race_number,
                    "payload": row.payload,
                    "created_at": self._iso(row.created_at),
                }
                for row in races
            ],
            "horses": [{"horse_id": row.horse_id, "horse_name": row.horse_name, "payload": row.payload} for row in horses],
            "race_entries": [
                {
                    "race_id": row.race_id,
                    "horse_id": row.horse_id,
                    "horse_number": row.horse_number,
                    "frame_number": row.frame_number,
                    "scratched": row.scratched,
                    "payload": row.payload,
                }
                for row in race_entries
            ],
            "analysis_runs": [
                {
                    "id": row.id,
                    "race_id": row.race_id,
                    "provider_name": row.provider_name,
                    "parse_status": row.parse_status,
                    "recognized_entries": row.recognized_entries,
                    "expected_entries": row.expected_entries,
                    "issue_count": row.issue_count,
                    "payload": row.payload,
                    "created_at": self._iso(row.created_at),
                }
                for row in analysis_runs
            ],
            "prediction_snapshots": [self._snapshot_payload(row) for row in prediction_snapshots],
            "challenger_predictions": [
                {
                    "prediction_snapshot_id": row.prediction_snapshot_id,
                    "model_version": row.model_version,
                    "feature_version": row.feature_version,
                    "target": row.target,
                    "predicted_value": row.predicted_value,
                    "metadata_json": row.metadata_json,
                    "created_at": self._iso(row.created_at),
                }
                for row in challenger_predictions
            ],
            "simulations": [
                {
                    "id": row.id,
                    "race_id": row.race_id,
                    "iterations": row.iterations,
                    "seed": row.seed,
                    "payload": row.payload,
                    "created_at": self._iso(row.created_at),
                }
                for row in simulations
            ],
            "user_predictions": [
                {
                    "id": row.id,
                    "race_id": row.race_id,
                    "horse_number": row.horse_number,
                    "payload": row.payload,
                    "prediction_timestamp": self._iso(row.prediction_timestamp),
                }
                for row in user_predictions
            ],
            "race_results": [
                {
                    "race_id": row.race_id,
                    "payload": row.payload,
                    "result_timestamp": row.result_timestamp,
                    "stored_at": self._iso(row.stored_at),
                }
                for row in race_results
            ],
            "prediction_reviews": [
                {
                    "id": row.id,
                    "race_id": row.race_id,
                    "payload": row.payload,
                    "created_at": self._iso(row.created_at),
                }
                for row in prediction_reviews
            ],
            "past_performances": [
                {
                    "id": row.id,
                    "horse_id": row.horse_id,
                    "observed_in_race_id": row.observed_in_race_id,
                    "race_date": row.race_date,
                    "racecourse": row.racecourse,
                    "race_name": row.race_name,
                    "finish_position": row.finish_position,
                    "payload": row.payload,
                    "stored_at": self._iso(row.stored_at),
                }
                for row in past_performances
            ],
            "model_evaluations": [
                {
                    "model_version": row.model_version,
                    "feature_version": row.feature_version,
                    "evaluation_period": row.evaluation_period,
                    "brier_score": row.brier_score,
                    "log_loss": row.log_loss,
                    "calibration_error": row.calibration_error,
                    "top3_hit_rate": row.top3_hit_rate,
                    "training_start": row.training_start,
                    "training_end": row.training_end,
                    "evaluation_start": row.evaluation_start,
                    "evaluation_end": row.evaluation_end,
                    "top3_recall": row.top3_recall,
                    "winner_rank_mean": row.winner_rank_mean,
                    "prediction_top3_mean_finish": row.prediction_top3_mean_finish,
                    "relative_finish_error": row.relative_finish_error,
                    "sample_count": row.sample_count,
                    "race_count": row.race_count,
                    "horse_count": row.horse_count,
                    "evaluation_variant": row.evaluation_variant,
                    "metrics_json": row.metrics_json,
                    "evaluated_at": self._iso(row.evaluated_at),
                }
                for row in model_evaluations
            ],
            "training_runs": [
                {
                    "model_version": row.model_version,
                    "trained_at": self._iso(row.trained_at),
                    "training_start": row.training_start,
                    "training_end": row.training_end,
                    "feature_version": row.feature_version,
                    "label": row.label,
                    "status": row.status,
                    "row_count": row.row_count,
                    "race_count": row.race_count,
                    "metrics_json": row.metrics_json,
                }
                for row in training_runs
            ],
            "qualitative_evidence": [
                {
                    "source_type": row.source_type,
                    "source_name": row.source_name,
                    "source_url": row.source_url,
                    "published_at": self._iso(row.published_at),
                    "author": row.author,
                    "race_id": row.race_id,
                    "horse_id": row.horse_id,
                    "raw_text": row.raw_text,
                    "normalized_text": row.normalized_text,
                    "confidence": row.confidence,
                    "content_hash": row.content_hash,
                    "collected_at": self._iso(row.collected_at),
                }
                for row in qualitative_evidence
            ],
            "source_reliability": [
                {
                    "source_type": row.source_type,
                    "source_name": row.source_name,
                    "reliability": row.reliability,
                    "sample_count": row.sample_count,
                    "metrics_json": row.metrics_json,
                    "updated_at": self._iso(row.updated_at),
                }
                for row in source_reliability
            ],
        }
        return {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "format_version": self.EXPORT_FORMAT_VERSION,
            "counts": {key: len(value) for key, value in data.items()},
            "data": data,
        }

    def training_dataset(self) -> dict:
        reviews = self._latest_reviews_by_race()
        races = {row.race_id: row for row in self.session.scalars(select(Race)).all()}
        snapshots = self._latest_snapshots_by_race_and_horse()
        result_entries = {
            (row.race_id, row.horse_number): row
            for row in self.session.scalars(select(RaceResultEntry)).all()
        }
        rows = []
        feature_columns: set[str] = set()
        excluded_counts = {"unsupported_race_type": 0, "invalid_prediction_time": 0}
        for review in reviews:
            race = races.get(review.race_id)
            if race is None or not is_supported_flat_race(
                self._field_value(race.payload or {}, "surface"),
                self._field_value(race.payload or {}, "race_class"),
            ):
                excluded_counts["unsupported_race_type"] += 1
                continue
            for review_row in (review.payload or {}).get("rows", []):
                horse_number = review_row.get("horse_number")
                actual_win = review_row.get("actual_win")
                if horse_number is None or actual_win is None:
                    continue
                snapshot = snapshots.get((review.race_id, int(horse_number)))
                if snapshot is None:
                    excluded_counts["invalid_prediction_time"] += 1
                    continue
                lead_minutes = prediction_lead_minutes(
                    race.race_date,
                    self._field_value(race.payload or {}, "start_time"),
                    snapshot.prediction_timestamp,
                )
                if lead_minutes is None or lead_minutes <= 0:
                    excluded_counts["invalid_prediction_time"] += 1
                    continue
                features = self._training_features(snapshot.raw_features or {})
                feature_columns.update(features.keys())
                finish_position = review_row.get("finish_position")
                result_entry = result_entries.get((review.race_id, int(horse_number)))
                field_size = (
                    result_entry.field_size
                    if result_entry is not None and result_entry.field_size is not None
                    else self._field_value(race.payload or {}, "field_size")
                )
                relative_value = (
                    result_entry.relative_finish
                    if result_entry is not None and result_entry.relative_finish is not None
                    else relative_finish(finish_position, field_size)
                )
                rows.append(
                    {
                        "race_id": review.race_id,
                        "race_date": race.race_date if race else None,
                        "racecourse": race.racecourse if race else None,
                        "race_number": race.race_number if race else None,
                        "horse_id": snapshot.horse_id,
                        "horse_number": int(horse_number),
                        "horse_name": review_row.get("horse_name") or (snapshot.prediction_payload or {}).get("horse_name"),
                        "snapshot_id": snapshot.id,
                        "prediction_timestamp": self._iso(snapshot.prediction_timestamp),
                        "review_created_at": self._iso(review.created_at),
                        "prediction_lead_minutes": round(lead_minutes, 3),
                        "model_version": snapshot.model_version,
                        "feature_version": snapshot.feature_version,
                        "ai_win_probability": snapshot.ai_win_probability,
                        "ai_top2_probability": snapshot.ai_top2_probability,
                        "ai_top3_probability": snapshot.ai_top3_probability,
                        "base_score": (snapshot.prediction_payload or {}).get("base_score"),
                        "confidence": (snapshot.prediction_payload or {}).get("confidence"),
                        "finish_position": finish_position,
                        "field_size": field_size,
                        "relative_finish": relative_value,
                        "actual_win": float(actual_win),
                        "actual_top3": bool(finish_position is not None and int(finish_position) <= 3),
                        "result_status": review_row.get("result_status"),
                        "features": features,
                    }
                )
        return {
            "row_count": len(rows),
            "race_count": len({row["race_id"] for row in rows}),
            "feature_columns": sorted(feature_columns),
            "label_columns": ["actual_win", "actual_top3", "finish_position", "relative_finish"],
            "excluded_counts": excluded_counts,
            "rows": rows,
            "note": "1行=1レース1頭です。最新reviewとprediction_snapshots.raw_featuresを結合しています。",
        }

    def import_data(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("インポートJSONの形式が正しくありません。")
        if payload.get("format_version") != self.EXPORT_FORMAT_VERSION:
            raise ValueError("未対応のexport形式です。")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError("インポートJSONにdataがありません。")

        stats = {
            "format_version": self.EXPORT_FORMAT_VERSION,
            "imported": {},
            "updated": {},
            "skipped": {},
        }
        try:
            for item in data.get("races", []):
                existing = self.session.get(Race, item.get("race_id"))
                if existing is None:
                    existing = Race(race_id=item.get("race_id"))
                    self._count_import(stats, "races", "imported")
                else:
                    self._count_import(stats, "races", "updated")
                existing.race_date = item.get("race_date")
                existing.racecourse = item.get("racecourse")
                existing.race_number = item.get("race_number")
                existing.payload = item.get("payload") or {}
                existing.created_at = self._parse_datetime(item.get("created_at")) or existing.created_at
                self.session.add(existing)

            for item in data.get("horses", []):
                existing = self.session.get(Horse, item.get("horse_id"))
                if existing is None:
                    existing = Horse(horse_id=item.get("horse_id"))
                    self._count_import(stats, "horses", "imported")
                else:
                    self._count_import(stats, "horses", "updated")
                existing.horse_name = item.get("horse_name")
                existing.payload = item.get("payload") or {}
                self.session.add(existing)

            self.session.flush()

            for item in data.get("race_entries", []):
                existing = self.session.scalar(
                    select(RaceEntry).where(
                        RaceEntry.race_id == item.get("race_id"),
                        RaceEntry.horse_number == item.get("horse_number"),
                    )
                )
                if existing is None:
                    existing = RaceEntry(race_id=item.get("race_id"), horse_number=item.get("horse_number"))
                    self._count_import(stats, "race_entries", "imported")
                else:
                    self._count_import(stats, "race_entries", "updated")
                existing.horse_id = item.get("horse_id")
                existing.frame_number = item.get("frame_number")
                existing.scratched = bool(item.get("scratched", False))
                existing.payload = item.get("payload") or {}
                self.session.add(existing)

            for item in data.get("analysis_runs", []):
                created_at = self._parse_datetime(item.get("created_at"))
                exists = created_at is not None and self.session.scalar(
                    select(AnalysisRun).where(
                        AnalysisRun.race_id == item.get("race_id"),
                        AnalysisRun.provider_name == item.get("provider_name"),
                        AnalysisRun.created_at == created_at,
                    )
                )
                if exists:
                    self._count_import(stats, "analysis_runs", "skipped")
                    continue
                self.session.add(
                    AnalysisRun(
                        race_id=item.get("race_id"),
                        provider_name=item.get("provider_name"),
                        parse_status=item.get("parse_status"),
                        recognized_entries=item.get("recognized_entries"),
                        expected_entries=item.get("expected_entries"),
                        issue_count=item.get("issue_count"),
                        payload=item.get("payload") or {},
                        created_at=created_at or datetime.now(timezone.utc),
                    )
                )
                self._count_import(stats, "analysis_runs", "imported")

            snapshot_id_map: dict[int, int] = {}
            for item in data.get("prediction_snapshots", []):
                prediction_timestamp = self._parse_datetime(item.get("prediction_timestamp"))
                snapshot = prediction_timestamp is not None and self.session.scalar(
                    select(PredictionSnapshot).where(
                        PredictionSnapshot.race_id == item.get("race_id"),
                        PredictionSnapshot.horse_id == item.get("horse_id"),
                        PredictionSnapshot.horse_number == item.get("horse_number"),
                        PredictionSnapshot.prediction_timestamp == prediction_timestamp,
                        PredictionSnapshot.simulation_seed == item.get("simulation_seed"),
                    )
                )
                if snapshot:
                    if item.get("id") is not None:
                        snapshot_id_map[int(item["id"])] = snapshot.id
                    self._save_prediction_metric(snapshot, snapshot.prediction_payload or {})
                    self._count_import(stats, "prediction_snapshots", "skipped")
                    continue
                snapshot = PredictionSnapshot(
                    race_id=item.get("race_id"),
                    horse_id=item.get("horse_id"),
                    horse_number=item.get("horse_number"),
                    prediction_timestamp=prediction_timestamp or datetime.now(timezone.utc),
                    model_version=item.get("model_version"),
                    feature_version=item.get("feature_version"),
                    simulation_seed=item.get("simulation_seed"),
                    ai_win_probability=item.get("ai_win_probability"),
                    ai_top2_probability=item.get("ai_top2_probability"),
                    ai_top3_probability=item.get("ai_top3_probability"),
                    raw_features=item.get("raw_features") or {},
                    prediction_payload=item.get("prediction") or {},
                )
                self.session.add(snapshot)
                self.session.flush()
                if item.get("id") is not None:
                    snapshot_id_map[int(item["id"])] = snapshot.id
                self._save_prediction_metric(snapshot, snapshot.prediction_payload)
                self._count_import(stats, "prediction_snapshots", "imported")

            for item in data.get("challenger_predictions", []):
                snapshot_id = snapshot_id_map.get(int(item.get("prediction_snapshot_id") or 0))
                if snapshot_id is None:
                    self._count_import(stats, "challenger_predictions", "skipped")
                    continue
                existing = self.session.scalar(
                    select(ChallengerPrediction).where(
                        ChallengerPrediction.prediction_snapshot_id == snapshot_id,
                        ChallengerPrediction.model_version == item.get("model_version"),
                        ChallengerPrediction.target == item.get("target"),
                    )
                )
                if existing is None:
                    existing = ChallengerPrediction(
                        prediction_snapshot_id=snapshot_id,
                        model_version=item.get("model_version"),
                        target=item.get("target"),
                    )
                    self._count_import(stats, "challenger_predictions", "imported")
                else:
                    self._count_import(stats, "challenger_predictions", "updated")
                existing.feature_version = item.get("feature_version")
                existing.predicted_value = item.get("predicted_value")
                existing.metadata_json = item.get("metadata_json") or {}
                existing.created_at = self._parse_datetime(item.get("created_at")) or existing.created_at
                self.session.add(existing)

            for item in data.get("simulations", []):
                created_at = self._parse_datetime(item.get("created_at"))
                exists = created_at is not None and self.session.scalar(
                    select(Simulation).where(
                        Simulation.race_id == item.get("race_id"),
                        Simulation.created_at == created_at,
                        Simulation.seed == item.get("seed"),
                        Simulation.iterations == item.get("iterations"),
                    )
                )
                if exists:
                    self._count_import(stats, "simulations", "skipped")
                    continue
                self.session.add(
                    Simulation(
                        race_id=item.get("race_id"),
                        iterations=item.get("iterations"),
                        seed=item.get("seed"),
                        payload=item.get("payload") or {},
                        created_at=created_at or datetime.now(timezone.utc),
                    )
                )
                self._count_import(stats, "simulations", "imported")

            for item in data.get("user_predictions", []):
                prediction_timestamp = self._parse_datetime(item.get("prediction_timestamp"))
                exists = prediction_timestamp is not None and self.session.scalar(
                    select(UserPrediction).where(
                        UserPrediction.race_id == item.get("race_id"),
                        UserPrediction.horse_number == item.get("horse_number"),
                        UserPrediction.prediction_timestamp == prediction_timestamp,
                    )
                )
                if exists:
                    self._count_import(stats, "user_predictions", "skipped")
                    continue
                self.session.add(
                    UserPrediction(
                        race_id=item.get("race_id"),
                        horse_number=item.get("horse_number"),
                        payload=item.get("payload") or {},
                        prediction_timestamp=prediction_timestamp or datetime.now(timezone.utc),
                    )
                )
                self._count_import(stats, "user_predictions", "imported")

            for item in data.get("race_results", []):
                existing = self.session.get(RaceResult, item.get("race_id"))
                if existing is None:
                    existing = RaceResult(race_id=item.get("race_id"))
                    self._count_import(stats, "race_results", "imported")
                else:
                    self._count_import(stats, "race_results", "updated")
                existing.payload = item.get("payload") or {}
                existing.result_timestamp = item.get("result_timestamp")
                existing.stored_at = self._parse_datetime(item.get("stored_at")) or existing.stored_at
                self.session.add(existing)
                self.session.flush()
                self._save_result_entries(RaceResultData.model_validate(existing.payload))

            for item in data.get("prediction_reviews", []):
                created_at = self._parse_datetime(item.get("created_at"))
                exists = created_at is not None and self.session.scalar(
                    select(PredictionReview).where(
                        PredictionReview.race_id == item.get("race_id"),
                        PredictionReview.created_at == created_at,
                    )
                )
                if exists:
                    self._count_import(stats, "prediction_reviews", "skipped")
                    continue
                self.session.add(
                    PredictionReview(
                        race_id=item.get("race_id"),
                        payload=item.get("payload") or {},
                        created_at=created_at or datetime.now(timezone.utc),
                    )
                )
                self._count_import(stats, "prediction_reviews", "imported")

            for item in data.get("past_performances", []):
                existing = self.session.scalar(
                    select(PastPerformance).where(
                        PastPerformance.horse_id == item.get("horse_id"),
                        PastPerformance.race_date == item.get("race_date"),
                        PastPerformance.racecourse == item.get("racecourse"),
                        PastPerformance.race_name == item.get("race_name"),
                    )
                )
                if existing is None:
                    existing = PastPerformance(
                        horse_id=item.get("horse_id"),
                        race_date=item.get("race_date"),
                        racecourse=item.get("racecourse"),
                        race_name=item.get("race_name"),
                    )
                    self._count_import(stats, "past_performances", "imported")
                else:
                    self._count_import(stats, "past_performances", "updated")
                existing.observed_in_race_id = item.get("observed_in_race_id")
                existing.finish_position = item.get("finish_position")
                existing.payload = item.get("payload") or {}
                existing.stored_at = self._parse_datetime(item.get("stored_at")) or existing.stored_at
                self.session.add(existing)

            for item in data.get("qualitative_evidence", []):
                existing = self.session.scalar(
                    select(QualitativeEvidence).where(
                        QualitativeEvidence.content_hash == item.get("content_hash"),
                        QualitativeEvidence.race_id == item.get("race_id"),
                        QualitativeEvidence.horse_id == item.get("horse_id"),
                    )
                )
                if existing is None:
                    existing = QualitativeEvidence(
                        content_hash=item.get("content_hash"),
                        race_id=item.get("race_id"),
                        horse_id=item.get("horse_id"),
                    )
                    self._count_import(stats, "qualitative_evidence", "imported")
                else:
                    self._count_import(stats, "qualitative_evidence", "updated")
                existing.source_type = item.get("source_type")
                existing.source_name = item.get("source_name")
                existing.source_url = item.get("source_url")
                existing.published_at = self._parse_datetime(item.get("published_at"))
                existing.author = item.get("author")
                existing.raw_text = item.get("raw_text")
                existing.normalized_text = item.get("normalized_text")
                existing.confidence = item.get("confidence")
                existing.collected_at = self._parse_datetime(item.get("collected_at")) or existing.collected_at
                self.session.add(existing)

            for item in data.get("source_reliability", []):
                existing = self.session.scalar(
                    select(SourceReliability).where(
                        SourceReliability.source_type == item.get("source_type"),
                        SourceReliability.source_name == item.get("source_name"),
                    )
                )
                if existing is None:
                    existing = SourceReliability(
                        source_type=item.get("source_type"),
                        source_name=item.get("source_name"),
                    )
                    self._count_import(stats, "source_reliability", "imported")
                else:
                    self._count_import(stats, "source_reliability", "updated")
                existing.reliability = item.get("reliability")
                existing.sample_count = int(item.get("sample_count") or 0)
                existing.metrics_json = item.get("metrics_json") or {}
                existing.updated_at = self._parse_datetime(item.get("updated_at")) or existing.updated_at
                self.session.add(existing)

            for item in data.get("training_runs", []):
                trained_at = self._parse_datetime(item.get("trained_at"))
                exists = trained_at is not None and self.session.scalar(
                    select(TrainingRun).where(
                        TrainingRun.model_version == item.get("model_version"),
                        TrainingRun.trained_at == trained_at,
                    )
                )
                if exists:
                    self._count_import(stats, "training_runs", "skipped")
                    continue
                self.session.add(
                    TrainingRun(
                        model_version=item.get("model_version"),
                        trained_at=trained_at or datetime.now(timezone.utc),
                        training_start=item.get("training_start"),
                        training_end=item.get("training_end"),
                        feature_version=item.get("feature_version"),
                        label=item.get("label"),
                        status=item.get("status") or "unknown",
                        row_count=int(item.get("row_count") or 0),
                        race_count=int(item.get("race_count") or 0),
                        metrics_json=item.get("metrics_json") or {},
                    )
                )
                self._count_import(stats, "training_runs", "imported")

            for item in data.get("model_evaluations", []):
                evaluated_at = self._parse_datetime(item.get("evaluated_at"))
                exists = evaluated_at is not None and self.session.scalar(
                    select(ModelEvaluation).where(
                        ModelEvaluation.model_version == item.get("model_version"),
                        ModelEvaluation.evaluated_at == evaluated_at,
                    )
                )
                if exists:
                    self._count_import(stats, "model_evaluations", "skipped")
                    continue
                self.session.add(
                    ModelEvaluation(
                        model_version=item.get("model_version"),
                        feature_version=item.get("feature_version"),
                        evaluation_period=item.get("evaluation_period") or "unknown..unknown",
                        brier_score=item.get("brier_score"),
                        log_loss=item.get("log_loss"),
                        calibration_error=item.get("calibration_error"),
                        top3_hit_rate=item.get("top3_hit_rate"),
                        training_start=item.get("training_start"),
                        training_end=item.get("training_end"),
                        evaluation_start=item.get("evaluation_start"),
                        evaluation_end=item.get("evaluation_end"),
                        top3_recall=item.get("top3_recall"),
                        winner_rank_mean=item.get("winner_rank_mean"),
                        prediction_top3_mean_finish=item.get("prediction_top3_mean_finish"),
                        relative_finish_error=item.get("relative_finish_error"),
                        sample_count=int(item.get("sample_count") or 0),
                        race_count=int(item.get("race_count") or 0),
                        horse_count=int(item.get("horse_count") or 0),
                        evaluation_variant=item.get("evaluation_variant"),
                        metrics_json=item.get("metrics_json") or {},
                        evaluated_at=evaluated_at or datetime.now(timezone.utc),
                    )
                )
                self._count_import(stats, "model_evaluations", "imported")

            self.session.commit()
        except Exception:
            self.session.rollback()
            raise
        return stats

    def latest_snapshot_predictions(self, race_id: str) -> list[PredictionData]:
        rows = self._latest_snapshot_rows(
            race_id,
            len(self.session.scalars(select(RaceEntry).where(RaceEntry.race_id == race_id)).all()) or 50,
        )
        return [PredictionData.model_validate(row.prediction_payload) for row in reversed(rows)]

    def has_prediction_snapshot(self, race_id: str) -> bool:
        return self.session.scalar(
            select(PredictionSnapshot.id).where(PredictionSnapshot.race_id == race_id).limit(1)
        ) is not None

    def predicted_race_id(self, race_date: str, racecourse: str, race_number: int) -> str | None:
        return self.session.scalar(
            select(Race.race_id)
            .join(PredictionSnapshot, PredictionSnapshot.race_id == Race.race_id)
            .where(
                Race.race_date == race_date,
                Race.racecourse == racecourse,
                Race.race_number == int(race_number),
            )
            .limit(1)
        )

    def _latest_analysis_run(self, race_id: str) -> AnalysisRun | None:
        return self.session.scalar(
            select(AnalysisRun).where(AnalysisRun.race_id == race_id).order_by(AnalysisRun.created_at.desc())
        )

    def _analysis_run_payload(self, row: AnalysisRun) -> dict:
        return {
            "id": row.id,
            "race_id": row.race_id,
            "provider_name": row.provider_name,
            "parse_status": row.parse_status,
            "recognized_entries": row.recognized_entries,
            "expected_entries": row.expected_entries,
            "issue_count": row.issue_count,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "payload": row.payload,
        }

    def _iso(self, value) -> str | None:
        return value.isoformat() if value else None

    def _parse_datetime(self, value) -> datetime | None:
        if isinstance(value, datetime):
            return value
        if not value:
            return None
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    def _count_import(self, stats: dict, table: str, bucket: str) -> None:
        stats[bucket][table] = stats[bucket].get(table, 0) + 1

    def _result_source(self, payload: dict | None) -> str | None:
        if not payload:
            return None
        entries = payload.get("entries") or []
        if entries and all(item.get("result_status") == "manual" for item in entries):
            return "manual"
        return "provider"

    def _latest_snapshot_rows(self, race_id: str, limit: int) -> list[PredictionSnapshot]:
        return self.session.scalars(
            select(PredictionSnapshot)
            .where(PredictionSnapshot.race_id == race_id)
            .order_by(PredictionSnapshot.id.desc())
            .limit(limit)
        ).all()

    def _latest_snapshots_by_race_and_horse(self) -> dict[tuple[str, int], PredictionSnapshot]:
        rows = self.session.scalars(
            select(PredictionSnapshot)
            .outerjoin(RaceResult, RaceResult.race_id == PredictionSnapshot.race_id)
            .where(
                or_(
                    RaceResult.race_id.is_(None),
                    PredictionSnapshot.prediction_timestamp <= RaceResult.stored_at,
                )
            )
            .order_by(PredictionSnapshot.prediction_timestamp.desc(), PredictionSnapshot.id.desc())
        ).all()
        races = {row.race_id: row for row in self.session.scalars(select(Race)).all()}
        latest: dict[tuple[str, int], PredictionSnapshot] = {}
        for row in rows:
            race = races.get(row.race_id)
            if race is not None:
                lead = prediction_lead_minutes(
                    race.race_date,
                    self._field_value(race.payload or {}, "start_time"),
                    row.prediction_timestamp,
                )
                if lead is None or lead <= 0:
                    continue
            latest.setdefault((row.race_id, row.horse_number), row)
        return latest

    def _training_features(self, raw_features: dict) -> dict:
        return {
            key: value
            for key, value in raw_features.items()
            if value is None or isinstance(value, (str, int, float, bool))
        }

    def _snapshot_payload(self, row: PredictionSnapshot) -> dict:
        return {
            "id": row.id,
            "race_id": row.race_id,
            "horse_id": row.horse_id,
            "horse_number": row.horse_number,
            "prediction_timestamp": row.prediction_timestamp.isoformat() if row.prediction_timestamp else None,
            "model_version": row.model_version,
            "feature_version": row.feature_version,
            "simulation_seed": row.simulation_seed,
            "ai_win_probability": row.ai_win_probability,
            "ai_top2_probability": row.ai_top2_probability,
            "ai_top3_probability": row.ai_top3_probability,
            "raw_features": row.raw_features,
            "prediction": row.prediction_payload,
        }

    def save_user_predictions(self, race_id: str, predictions: dict[int, UserPredictionInput]) -> int:
        saved = 0
        for horse_number, payload in predictions.items():
            if not self._has_user_prediction_value(payload):
                continue
            self.session.add(UserPrediction(race_id=race_id, horse_number=int(horse_number), payload=payload.model_dump(mode="json")))
            saved += 1
        self.session.commit()
        return saved

    def latest_user_predictions(self, race_id: str) -> dict[int, UserPredictionInput]:
        rows = self.session.scalars(
            select(UserPrediction)
            .where(UserPrediction.race_id == race_id)
            .order_by(UserPrediction.prediction_timestamp.desc(), UserPrediction.id.desc())
        ).all()
        latest: dict[int, UserPredictionInput] = {}
        for row in rows:
            if row.horse_number in latest:
                continue
            prediction = UserPredictionInput.model_validate(row.payload)
            if self._has_user_prediction_value(prediction):
                latest[row.horse_number] = prediction
        return latest

    def _has_user_prediction_value(self, prediction: UserPredictionInput) -> bool:
        return any(
            value not in (None, "")
            for value in prediction.model_dump(mode="json").values()
        )

    def save_result(self, result: RaceResultData) -> None:
        row = self.session.get(RaceResult, result.race_id) or RaceResult(race_id=result.race_id)
        row.payload = result.model_dump(mode="json")
        row.result_timestamp = result.result_timestamp
        self.session.add(row)
        self.session.flush()
        self._save_result_entries(result)
        self._save_result_past_performances(result)
        self.session.commit()

    def _save_result_entries(self, result: RaceResultData) -> None:
        race = self.session.get(Race, result.race_id)
        configured_field_size = self._field_value(race.payload or {}, "field_size") if race else None
        field_size = int(configured_field_size or len(result.entries)) if result.entries else None
        entries = {
            entry.horse_number: entry
            for entry in self.session.scalars(select(RaceEntry).where(RaceEntry.race_id == result.race_id)).all()
        }
        for item in result.entries:
            row = self.session.scalar(
                select(RaceResultEntry).where(
                    RaceResultEntry.race_id == result.race_id,
                    RaceResultEntry.horse_number == item.horse_number,
                )
            )
            if row is None:
                row = RaceResultEntry(race_id=result.race_id, horse_number=item.horse_number)
            race_entry = entries.get(item.horse_number)
            row.horse_id = race_entry.horse_id if race_entry else None
            row.finish_position = item.finish_position
            row.result_status = item.result_status
            row.finish_time = item.finish_time
            row.margin = item.margin
            row.final_three_furlongs = item.final_three_furlongs
            row.corner_positions = item.corner_positions
            row.horse_weight = item.horse_weight
            row.horse_weight_change = item.horse_weight_change
            row.jockey_name = item.jockey_name
            row.popularity = item.popularity
            row.field_size = field_size
            row.relative_finish = relative_finish(item.finish_position, field_size)
            row.relative_finish_category = relative_finish_category(row.relative_finish)
            self.session.add(row)

    def save_training_run(
        self,
        model_version: str,
        feature_version: str | None,
        dataset: dict,
        metrics: dict,
    ) -> TrainingRun:
        race_dates = sorted(
            str(row["race_date"])
            for row in dataset.get("rows", [])
            if row.get("race_date") not in (None, "")
        )
        run = TrainingRun(
            model_version=model_version,
            training_start=race_dates[0] if race_dates else None,
            training_end=race_dates[-1] if race_dates else None,
            feature_version=feature_version,
            label=metrics.get("label"),
            status=str(metrics.get("status") or "unknown"),
            row_count=int(metrics.get("row_count") or dataset.get("row_count") or 0),
            race_count=int(metrics.get("race_count") or dataset.get("race_count") or 0),
            metrics_json=metrics,
        )
        self.session.add(run)
        self.session.flush()

        holdout = metrics.get("race_holdout") or {}
        if holdout.get("status") == "evaluated":
            period = "..".join(
                value or "unknown"
                for value in (run.training_start, run.training_end)
            )
            self.session.add(
                ModelEvaluation(
                    model_version=model_version,
                    feature_version=feature_version,
                    training_run_id=run.id,
                    training_start=run.training_start,
                    training_end=run.training_end,
                    evaluation_period=period,
                    brier_score=holdout.get("model_brier_score", holdout.get("blended_brier_score")),
                    log_loss=holdout.get("model_log_loss", holdout.get("blended_log_loss")),
                    calibration_error=holdout.get(
                        "model_calibration_error", holdout.get("blended_calibration_error")
                    ),
                    top3_hit_rate=holdout.get("blended_top3_hit_rate"),
                    sample_count=int(holdout.get("sample_count") or 0),
                    race_count=int(holdout.get("fold_count") or 0),
                    horse_count=int(holdout.get("sample_count") or 0),
                    evaluation_variant="walk_forward_logistic",
                    metrics_json=holdout,
                )
            )
        self.session.commit()
        return run

    def save_model_evaluation(
        self,
        model_version: str,
        feature_version: str | None,
        evaluation: dict,
        training_run_id: int | None = None,
    ) -> ModelEvaluation | None:
        if evaluation.get("status") != "evaluated":
            return None
        period = "..".join(
            (evaluation.get("evaluation_start") or "unknown", evaluation.get("evaluation_end") or "unknown")
        )
        row = ModelEvaluation(
            model_version=model_version,
            feature_version=feature_version,
            training_run_id=training_run_id,
            training_start=evaluation.get("training_start"),
            training_end=evaluation.get("training_end"),
            evaluation_start=evaluation.get("evaluation_start"),
            evaluation_end=evaluation.get("evaluation_end"),
            evaluation_period=period,
            brier_score=evaluation.get("brier_score"),
            log_loss=evaluation.get("log_loss"),
            calibration_error=evaluation.get("calibration_error"),
            top3_hit_rate=evaluation.get("top3_recall"),
            top3_recall=evaluation.get("top3_recall"),
            winner_rank_mean=evaluation.get("winner_rank_mean"),
            prediction_top3_mean_finish=evaluation.get("prediction_top3_mean_finish"),
            relative_finish_error=evaluation.get("relative_finish_error"),
            sample_count=int(evaluation.get("horse_count") or 0),
            horse_count=int(evaluation.get("horse_count") or 0),
            race_count=int(evaluation.get("race_count") or 0),
            evaluation_variant="with_jev" if evaluation.get("include_jev") else "without_jev",
            metrics_json=evaluation,
        )
        self.session.add(row)
        self.session.commit()
        return row

    def _save_result_past_performances(self, result: RaceResultData) -> None:
        race = self.session.get(Race, result.race_id)
        if race is None:
            return
        race_payload = race.payload or {}
        race_name = self._field_value(race_payload, "race_name")
        surface = self._field_value(race_payload, "surface")
        distance = self._field_value(race_payload, "distance")
        track_condition = self._field_value(race_payload, "track_condition")
        grade = self._field_value(race_payload, "grade")
        race_class = self._field_value(race_payload, "race_class")
        field_size = self._field_value(race_payload, "field_size")
        field_size = int(field_size or len(result.entries)) if result.entries else None
        snapshots = self._latest_snapshots_by_race_and_horse()
        entries = {
            entry.horse_number: entry
            for entry in self.session.scalars(select(RaceEntry).where(RaceEntry.race_id == result.race_id)).all()
        }
        winner_times = [
            self._finish_time_seconds(item.finish_time)
            for item in result.entries
            if item.finish_position == 1 and self._finish_time_seconds(item.finish_time) is not None
        ]
        winner_time = min(winner_times) if winner_times else None
        for result_entry in result.entries:
            entry = entries.get(result_entry.horse_number)
            if entry is None:
                continue
            existing = self.session.scalar(
                select(PastPerformance).where(
                    PastPerformance.horse_id == entry.horse_id,
                    PastPerformance.race_date == race.race_date,
                    PastPerformance.racecourse == race.racecourse,
                    PastPerformance.race_name == race_name,
                )
            )
            if existing is None:
                existing = PastPerformance(
                    horse_id=entry.horse_id,
                    observed_in_race_id=result.race_id,
                    race_date=race.race_date,
                    racecourse=race.racecourse,
                    race_name=race_name,
                )
            finish_time_seconds = self._finish_time_seconds(result_entry.finish_time)
            actual_performance = relative_finish(result_entry.finish_position, field_size)
            snapshot = snapshots.get((result.race_id, result_entry.horse_number))
            predicted_rank = (snapshot.prediction_payload or {}).get("final_rank") if snapshot else None
            expected_performance = relative_finish(predicted_rank, field_size)
            performance_residual = (
                actual_performance - expected_performance
                if actual_performance is not None and expected_performance is not None
                else None
            )
            result_values = {
                "race_date": race.race_date,
                "racecourse": race.racecourse,
                "race_name": race_name,
                "grade": grade,
                "race_class": race_class,
                "surface": surface,
                "distance": distance,
                "track_condition": track_condition,
                "field_size": field_size,
                "finish_position": result_entry.finish_position,
                "actual_performance": actual_performance,
                "expected_performance": expected_performance,
                "performance_residual": performance_residual,
                "result_status": result_entry.result_status,
                "finish_time": result_entry.finish_time,
                "time_behind_winner_seconds": (
                    round(max(0.0, finish_time_seconds - winner_time), 3)
                    if finish_time_seconds is not None and winner_time is not None
                    else None
                ),
                "result_margin": result_entry.margin,
                "final_three_furlongs": result_entry.final_three_furlongs,
                "corner_positions": result_entry.corner_positions,
                "horse_weight": result_entry.horse_weight,
                "horse_weight_change": result_entry.horse_weight_change,
                "jockey_name": result_entry.jockey_name,
                "popularity": result_entry.popularity,
            }
            payload = dict(existing.payload or {})
            payload.update({key: value for key, value in result_values.items() if value is not None})
            existing.finish_position = result_entry.finish_position
            existing.payload = payload
            self.session.add(existing)

    def _finish_time_seconds(self, value: object) -> float | None:
        if value in (None, ""):
            return None
        parts = str(value).strip().split(":")
        try:
            return int(parts[0]) * 60 + float(parts[1]) if len(parts) == 2 else float(parts[0])
        except (TypeError, ValueError):
            return None

    def saved_result(self, race_id: str) -> tuple[RaceResultData | None, str | None]:
        row = self.session.get(RaceResult, race_id)
        if row is None:
            return None, None
        return RaceResultData.model_validate(row.payload), self._result_source(row.payload)

    def save_review(self, race_id: str, review: dict) -> None:
        row = self.session.scalar(
            select(PredictionReview)
            .where(PredictionReview.race_id == race_id)
            .order_by(PredictionReview.created_at.desc(), PredictionReview.id.desc())
        )
        if row is None:
            row = PredictionReview(race_id=race_id)
        row.payload = review
        row.created_at = datetime.now(timezone.utc)
        self.session.add(row)
        self.session.commit()

    def save_past_performances(self, observed_in_race_id: str, histories: dict[str, list[dict]]) -> None:
        for horse_id, rows in histories.items():
            for row in rows:
                race_date = row.get("race_date")
                if not race_date:
                    continue
                existing = self.session.scalar(
                    select(PastPerformance).where(
                        PastPerformance.horse_id == horse_id,
                        PastPerformance.race_date == race_date,
                        PastPerformance.racecourse == row.get("racecourse"),
                        PastPerformance.race_name == row.get("race_name"),
                    )
                )
                if existing is None:
                    existing = PastPerformance(
                        horse_id=horse_id,
                        observed_in_race_id=observed_in_race_id,
                        race_date=race_date,
                        racecourse=row.get("racecourse"),
                        race_name=row.get("race_name"),
                    )
                existing.finish_position = row.get("finish_position")
                existing.payload = row
                self.session.add(existing)
        self.session.commit()

    def get_horse_history(self, horse_id: str, limit: int = 5, before_date: str | None = None) -> list[dict]:
        statement = select(PastPerformance).where(PastPerformance.horse_id == horse_id)
        if before_date:
            statement = statement.where(PastPerformance.race_date < before_date)
        rows = self.session.scalars(statement.order_by(PastPerformance.race_date.desc()).limit(limit)).all()
        return [row.payload for row in rows]

    def learning_summary(self, eligible_only: bool = False) -> dict:
        reviews = self._latest_reviews_by_race()
        if eligible_only:
            reviews = self._eligible_reviews_for_learning(reviews)
        payloads = [row.payload for row in reviews]
        ai_brier = [payload.get("ai_brier_score") for payload in payloads if payload.get("ai_brier_score") is not None]
        user_brier = [payload.get("user_brier_score") for payload in payloads if payload.get("user_brier_score") is not None]
        condition_breakdown = self._condition_breakdown(reviews)
        horse_breakdown = self._horse_breakdown(reviews)
        user_rating_breakdown = self._user_rating_breakdown(reviews)
        comparison_summary = self._comparison_summary(payloads)
        calibration_summary = self._calibration_summary(payloads)
        factor_attribution_summary = self._factor_attribution_summary(payloads)
        factor_calibration_summary = self._factor_calibration_summary(payloads)
        factor_weight_recommendations = self._factor_weight_recommendations(factor_attribution_summary, factor_calibration_summary)
        stability_error_summary = self._stability_error_summary(payloads)
        ranking_metric_summary = self._ranking_metric_summary(payloads)
        trend_summary = self._trend_summary(reviews)
        data_quality_summary = self._data_quality_summary()
        user_probability_quality_summary = self._user_probability_quality_summary(payloads)
        recent = [self._recent_review_payload(row) for row in sorted(reviews, key=lambda item: item.created_at, reverse=True)[:5]]
        return {
            "review_count": len(reviews),
            "ai_brier_score": round(mean(ai_brier), 6) if ai_brier else None,
            "user_brier_score": round(mean(user_brier), 6) if user_brier else None,
            "comparison_summary": comparison_summary,
            "trend_summary": trend_summary,
            "data_quality_summary": data_quality_summary,
            "user_probability_quality_summary": user_probability_quality_summary,
            "calibration_summary": calibration_summary,
            "learning_insights": self._learning_insights(
                comparison_summary,
                calibration_summary,
                factor_attribution_summary,
                factor_calibration_summary,
                factor_weight_recommendations,
                condition_breakdown,
                horse_breakdown,
                user_rating_breakdown,
            ),
            "recent_reviews": recent,
            "condition_breakdown": condition_breakdown,
            "horse_breakdown": horse_breakdown,
            "user_rating_breakdown": user_rating_breakdown,
            "factor_attribution_summary": factor_attribution_summary,
            "factor_calibration_summary": factor_calibration_summary,
            "factor_weight_recommendations": factor_weight_recommendations,
            "stability_error_summary": stability_error_summary,
            "ranking_metric_summary": ranking_metric_summary,
            "note": "十分なレビュー件数が溜まるまでは条件別精度の統計的な意味は限定的です。",
        }

    def _stability_error_summary(self, payloads: list[dict]) -> list[dict]:
        definitions = {
            "course_adjustment": lambda row: abs(float(row.get("course_adjustment") or 0.0)) >= 0.03,
            "surface_adjustment": lambda row: abs(float(row.get("surface_adjustment") or 0.0)) >= 0.03,
            "track_bias_adjustment": lambda row: abs(float(row.get("track_bias_adjustment") or 0.0)) >= 0.01,
            "pedigree_adjustment": lambda row: abs(float(row.get("pedigree_adjustment") or 0.0)) >= 0.01,
            "career_shallow": lambda row: bool((row.get("new_horse_flags") or {}).get("career_shallow")),
            "first_distance": lambda row: bool((row.get("new_horse_flags") or {}).get("first_distance")),
            "first_racecourse": lambda row: bool((row.get("new_horse_flags") or {}).get("first_racecourse")),
        }
        result = []
        all_rows = [row for payload in payloads for row in (payload.get("rows") or [])]
        for condition, predicate in definitions.items():
            matched = [row for row in all_rows if predicate(row) and row.get("ai_brier") is not None]
            if not matched:
                continue
            result.append(
                {
                    "condition": condition,
                    "count": len(matched),
                    "average_ai_brier": round(mean(float(row["ai_brier"]) for row in matched), 6),
                    "statistical_note": "sample_small" if len(matched) < 30 else "ok",
                }
            )
        return result

    def _ranking_metric_summary(self, payloads: list[dict]) -> dict:
        metrics = [payload.get("ranking_metrics") or {} for payload in payloads]
        winner_ranks = [item.get("winner_predicted_rank") for item in metrics if item.get("winner_predicted_rank") is not None]
        capture_rates = [item.get("top3_capture_rate") for item in metrics if item.get("top3_capture_rate") is not None]
        rank_errors = [item.get("mean_rank_error") for item in metrics if item.get("mean_rank_error") is not None]
        return {
            "race_count": len(metrics),
            "average_winner_predicted_rank": round(mean(winner_ranks), 6) if winner_ranks else None,
            "average_top3_capture_rate": round(mean(capture_rates), 6) if capture_rates else None,
            "average_rank_error": round(mean(rank_errors), 6) if rank_errors else None,
        }

    def active_factor_adjustments(self) -> dict[str, float]:
        reviews = self._eligible_reviews_for_learning(self._latest_reviews_by_race())
        payloads = [row.payload or {} for row in reviews]
        factor_attribution = self._factor_attribution_summary(payloads)
        factor_calibration = self._factor_calibration_summary(payloads)
        recommendations = self._factor_weight_recommendations(factor_attribution, factor_calibration)
        adjustments: dict[str, float] = {}
        for row in recommendations:
            if row.get("readiness") != "candidate":
                continue
            if row.get("deployment_status") != "champion":
                continue
            factor_key = row.get("factor_key")
            suggested_delta = row.get("suggested_delta")
            if not factor_key or suggested_delta is None:
                continue
            if factor_key == "基礎点":
                continue
            adjustments[factor_key] = round(max(-0.05, min(0.05, float(suggested_delta))), 6)
        return adjustments

    def _eligible_reviews_for_learning(self, reviews: list[PredictionReview]) -> list[PredictionReview]:
        race_ids = {review.race_id for review in reviews}
        if not race_ids:
            return []
        races = {
            race.race_id: race
            for race in self.session.scalars(select(Race).where(Race.race_id.in_(race_ids))).all()
        }
        eligible_race_ids: set[str] = set()
        snapshots = self.session.execute(
            select(PredictionSnapshot.race_id, PredictionSnapshot.prediction_timestamp)
            .where(PredictionSnapshot.race_id.in_(race_ids))
        ).all()
        for race_id, prediction_timestamp in snapshots:
            race = races.get(race_id)
            if race is None or race_id in eligible_race_ids:
                continue
            payload = race.payload or {}
            if not is_supported_flat_race(
                self._field_value(payload, "surface"),
                self._field_value(payload, "race_class"),
            ):
                continue
            if (prediction_lead_minutes(
                race.race_date,
                self._field_value(payload, "start_time"),
                prediction_timestamp,
            ) or 0) > 0:
                eligible_race_ids.add(race_id)
        return [review for review in reviews if review.race_id in eligible_race_ids]

    def _latest_reviews_by_race(self) -> list[PredictionReview]:
        rows = self.session.scalars(
            select(PredictionReview).order_by(PredictionReview.created_at, PredictionReview.id)
        ).all()
        latest: dict[str, PredictionReview] = {}
        for row in rows:
            latest[row.race_id] = row
        return list(latest.values())

    def _data_quality_summary(self) -> dict:
        runs = self.session.scalars(select(AnalysisRun)).all()
        parse_status_counts: dict[str, int] = {}
        provider_counts: dict[str, int] = {}
        feature_ratios = []
        missing_history_counts = []
        issue_counts = []
        unavailable_counts: dict[str, int] = {}
        for row in runs:
            parse_status_counts[row.parse_status] = parse_status_counts.get(row.parse_status, 0) + 1
            provider_counts[row.provider_name] = provider_counts.get(row.provider_name, 0) + 1
            issue_counts.append(row.issue_count)
            feature_quality = (row.payload or {}).get("feature_quality") or {}
            if feature_quality.get("available_feature_ratio") is not None:
                feature_ratios.append(float(feature_quality["available_feature_ratio"]))
            if feature_quality.get("missing_history_count") is not None:
                missing_history_counts.append(float(feature_quality["missing_history_count"]))
            for key, count in (feature_quality.get("unavailable_counts") or {}).items():
                unavailable_counts[key] = unavailable_counts.get(key, 0) + int(count)
        return {
            "analysis_run_count": len(runs),
            "parse_status_counts": parse_status_counts,
            "provider_counts": provider_counts,
            "average_issue_count": round(mean(issue_counts), 6) if issue_counts else None,
            "average_available_feature_ratio": round(mean(feature_ratios), 6) if feature_ratios else None,
            "average_missing_history_count": round(mean(missing_history_counts), 6) if missing_history_counts else None,
            "top_unavailable_features": [
                {"feature": key, "count": count}
                for key, count in sorted(unavailable_counts.items(), key=lambda item: item[1], reverse=True)[:5]
            ],
            "attention_runs": self._attention_analysis_runs(runs),
            "note": "解析品質は予測精度の前提条件です。特徴量充足率が低いレースは学習結果も参考値として扱います。",
        }

    def _attention_analysis_runs(self, runs: list[AnalysisRun]) -> list[dict]:
        def quality_score(row: AnalysisRun) -> tuple[float, int, float]:
            feature_quality = (row.payload or {}).get("feature_quality") or {}
            feature_ratio = feature_quality.get("available_feature_ratio")
            missing_history_count = feature_quality.get("missing_history_count") or 0
            feature_gap = 1.0 - float(feature_ratio) if feature_ratio is not None else 1.0
            return (float(row.issue_count) + feature_gap + float(missing_history_count) * 0.05, row.issue_count, feature_gap)

        rows = []
        for row in sorted(runs, key=quality_score, reverse=True)[:5]:
            race = self.session.get(Race, row.race_id)
            feature_quality = (row.payload or {}).get("feature_quality") or {}
            rows.append(
                {
                    "race_id": row.race_id,
                    "race_date": race.race_date if race else None,
                    "racecourse": race.racecourse if race else None,
                    "race_number": race.race_number if race else None,
                    "race_name": self._field_value(race.payload or {}, "race_name") if race else None,
                    "provider_name": row.provider_name,
                    "parse_status": row.parse_status,
                    "issue_count": row.issue_count,
                    "available_feature_ratio": feature_quality.get("available_feature_ratio"),
                    "missing_history_count": feature_quality.get("missing_history_count"),
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
            )
        return rows

    def _recent_review_payload(self, row: PredictionReview) -> dict:
        race = self.session.get(Race, row.race_id)
        return {
            "race_id": row.race_id,
            "race_date": race.race_date if race else None,
            "racecourse": race.racecourse if race else None,
            "race_number": race.race_number if race else None,
            "race_name": self._field_value(race.payload or {}, "race_name") if race else None,
            "ai_brier_score": row.payload.get("ai_brier_score"),
            "user_brier_score": row.payload.get("user_brier_score"),
            "comparison": row.payload.get("comparison"),
            "user_probability_summary": row.payload.get("user_probability_summary"),
            "conditions": self._race_conditions(race),
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }

    def _user_probability_quality_summary(self, payloads: list[dict]) -> dict:
        summaries = [payload.get("user_probability_summary") for payload in payloads if payload.get("user_probability_summary")]
        status_counts: dict[str, int] = {}
        totals = []
        coverages = []
        for summary in summaries:
            status = summary.get("status") or "unknown"
            status_counts[status] = status_counts.get(status, 0) + 1
            if summary.get("probability_total") is not None:
                totals.append(float(summary["probability_total"]))
            if summary.get("coverage") is not None:
                coverages.append(float(summary["coverage"]))
        return {
            "review_count": len(summaries),
            "status_counts": status_counts,
            "average_probability_total": round(mean(totals), 6) if totals else None,
            "average_coverage": round(mean(coverages), 6) if coverages else None,
            "note": "自分予想の勝率入力が100%付近で、入力率が高いほどAI/User比較を読みやすくなります。",
        }

    def _trend_summary(self, reviews: list[PredictionReview]) -> dict:
        ordered = sorted(reviews, key=lambda item: item.created_at, reverse=True)
        recent = ordered[:5]
        ai_values = [row.payload.get("ai_brier_score") for row in recent if row.payload.get("ai_brier_score") is not None]
        user_values = [row.payload.get("user_brier_score") for row in recent if row.payload.get("user_brier_score") is not None]
        return {
            "recent_count": len(recent),
            "ai_recent_brier_score": round(mean(ai_values), 6) if ai_values else None,
            "user_recent_brier_score": round(mean(user_values), 6) if user_values else None,
            "ai_direction": self._trend_direction(ordered, "ai_brier_score"),
            "user_direction": self._trend_direction(ordered, "user_brier_score"),
            "note": "直近5件の平均と、新しい半分/古い半分の比較です。4件未満は傾向判定を保留します。",
        }

    def _trend_direction(self, reviews: list[PredictionReview], key: str) -> str:
        values = [row.payload.get(key) for row in reviews if row.payload.get(key) is not None]
        if len(values) < 4:
            return "insufficient"
        midpoint = min(5, len(values)) // 2
        recent_average = mean(values[:midpoint])
        previous_average = mean(values[midpoint:min(5, len(values))])
        delta = recent_average - previous_average
        if abs(delta) < 0.005:
            return "flat"
        return "improving" if delta < 0 else "worsening"

    def _learning_insights(
        self,
        comparison_summary: dict,
        calibration_summary: dict,
        factor_attribution_summary: dict,
        factor_calibration_summary: dict,
        factor_weight_recommendations: list[dict],
        condition_breakdown: list[dict],
        horse_breakdown: list[dict],
        user_rating_breakdown: list[dict],
    ) -> list[dict]:
        insights = []
        delta = comparison_summary.get("average_brier_delta")
        if delta is not None:
            if delta > 0:
                insights.append({"type": "comparison", "message": "平均BrierではAI予測が自分予想を上回っています。"})
            elif delta < 0:
                insights.append({"type": "comparison", "message": "平均Brierでは自分予想がAI予測を上回っています。"})
            else:
                insights.append({"type": "comparison", "message": "平均BrierではAI予測と自分予想が同程度です。"})

        for source, label in (("ai", "AI"), ("user", "自分予想")):
            rows = calibration_summary.get(source, [])
            if not rows:
                continue
            worst = max(rows, key=lambda item: abs(item["calibration_gap"]))
            direction = "高め" if worst["calibration_gap"] > 0 else "低め"
            insights.append(
                {
                    "type": "calibration",
                    "message": f"{label}の{worst['bucket']}帯は実勝率に対して予測が{direction}です。",
                    "statistical_note": worst["statistical_note"],
                }
            )

        rank_overvalued = factor_attribution_summary.get("rank_overvalued_positive_factors") or []
        overvalued = rank_overvalued or factor_attribution_summary.get("overvalued_positive_factors") or []
        if overvalued:
            worst_factor = overvalued[0]
            insights.append(
                {
                    "type": "factor_attribution",
                    "message": (
                        f"{worst_factor['factor']} は実着順より過大に順位評価した要因として目立ちます。"
                        if rank_overvalued
                        else f"{worst_factor['factor']} は非勝ち馬を押し上げた要因として目立ちます。"
                    ),
                    "score": worst_factor.get("average_absolute_rank_error") or worst_factor.get("average_ai_brier"),
                    "statistical_note": worst_factor["statistical_note"],
                }
            )
        winner_negative = factor_attribution_summary.get("winner_negative_factors") or []
        if winner_negative:
            worst_factor = winner_negative[0]
            insights.append(
                {
                    "type": "factor_attribution",
                    "message": f"{worst_factor['factor']} は勝ち馬を下げた要因として確認できます。",
                    "score": worst_factor.get("average_ai_brier"),
                    "statistical_note": worst_factor["statistical_note"],
                }
            )
        if factor_weight_recommendations:
            recommendation = factor_weight_recommendations[0]
            insights.append(
                {
                    "type": "factor_tuning",
                    "message": f"{recommendation['factor']} は {recommendation['action']} の調整候補です。",
                    "suggested_delta": recommendation["suggested_delta"],
                    "statistical_note": recommendation["statistical_note"],
                }
            )
        factor_calibration = factor_calibration_summary.get("positive") or []
        if factor_calibration:
            worst = max(factor_calibration, key=lambda item: abs(item["calibration_gap"]))
            direction = "過大評価" if worst["calibration_gap"] > 0 else "過小評価"
            insights.append(
                {
                    "type": "factor_calibration",
                    "message": f"{worst['factor_key']} はファクター別校正で{direction}気味です。",
                    "calibration_gap": worst["calibration_gap"],
                    "statistical_note": worst["statistical_note"],
                }
            )

        for group_name, rows in (
            ("条件", condition_breakdown),
            ("馬条件", horse_breakdown),
            ("ユーザー評価", user_rating_breakdown),
        ):
            scored = [
                row
                for row in rows
                if row.get("user_brier_score") is not None or row.get("ai_brier_score") is not None
            ]
            if not scored:
                continue
            worst = max(
                scored,
                key=lambda row: max(row.get("user_brier_score") or 0, row.get("ai_brier_score") or 0),
            )
            score = worst.get("user_brier_score") if worst.get("user_brier_score") is not None else worst.get("ai_brier_score")
            insights.append(
                {
                    "type": "breakdown",
                    "message": f"{group_name}: {worst['dimension']}={worst['value']} はBrierが相対的に悪い傾向です。",
                    "score": score,
                    "statistical_note": worst["statistical_note"],
                }
            )
            if len(insights) >= 6:
                break
        return insights[:6]

    def _factor_calibration_summary(self, payloads: list[dict]) -> dict:
        return {
            "positive": self._factor_calibration_rows(payloads, "positive"),
            "negative": self._factor_calibration_rows(payloads, "negative"),
            "note": "ファクターが付いた馬の平均AI勝率と実勝率の差です。正のgapは過大評価、負のgapは過小評価を示します。",
        }

    def _factor_calibration_rows(self, payloads: list[dict], direction: str) -> list[dict]:
        buckets: dict[str, dict] = {}
        for payload in payloads:
            for row in payload.get("rows", []):
                actual_win = row.get("actual_win")
                ai_probability = row.get("ai_probability")
                ai_brier = row.get("ai_brier")
                if actual_win is None or ai_probability is None or ai_brier is None:
                    continue
                for impact in row.get("factor_impacts") or []:
                    if impact.get("direction") != direction:
                        continue
                    factor = impact.get("factor")
                    if not factor:
                        continue
                    factor_key = impact.get("factor_key") or self._factor_key(factor)
                    bucket = buckets.setdefault(
                        factor_key,
                        {
                            "factor_key": factor_key,
                            "factor": factor,
                            "direction": direction,
                            "count": 0,
                            "amounts": [],
                            "probabilities": [],
                            "actuals": [],
                            "briers": [],
                        },
                    )
                    bucket["count"] += 1
                    if impact.get("amount") is not None:
                        bucket["amounts"].append(float(impact["amount"]))
                    bucket["probabilities"].append(float(ai_probability))
                    bucket["actuals"].append(float(actual_win))
                    bucket["briers"].append(float(ai_brier))

        rows = []
        for bucket in buckets.values():
            probabilities = bucket["probabilities"]
            actuals = bucket["actuals"]
            rows.append(
                {
                    "factor_key": bucket["factor_key"],
                    "factor": bucket["factor"],
                    "direction": bucket["direction"],
                    "count": bucket["count"],
                    "average_amount": round(mean(bucket["amounts"]), 6) if bucket["amounts"] else None,
                    "average_ai_probability": round(mean(probabilities), 6),
                    "actual_win_rate": round(mean(actuals), 6),
                    "calibration_gap": round(mean(probabilities) - mean(actuals), 6),
                    "average_ai_brier": round(mean(bucket["briers"]), 6),
                    "statistical_note": "sample_small" if bucket["count"] < 30 else "ok",
                }
            )
        return sorted(rows, key=lambda item: abs(item["calibration_gap"]), reverse=True)[:10]

    def _factor_key(self, factor: str) -> str:
        import re

        stripped = factor.strip()
        stripped = re.sub(r"\s+[+-]\d+(?:\.\d+)?$", "", stripped)
        stripped = re.sub(r"[+-]\d+(?:\.\d+)?kg", "{kg}", stripped)
        stripped = re.sub(r"\d+(?:\.\d+)?m", "{meters}m", stripped)
        return stripped

    def _factor_weight_recommendations(self, factor_attribution_summary: dict, factor_calibration_summary: dict | None = None) -> list[dict]:
        candidates: dict[str, dict] = {}
        calibration_by_key = self._calibration_by_factor_key(factor_calibration_summary or {})
        for row in factor_attribution_summary.get("rank_overvalued_positive_factors") or []:
            factor_key = row.get("factor_key") or row["factor"]
            amount = abs(float(row.get("average_amount") or 0.0))
            candidates[factor_key] = self._factor_weight_candidate(
                factor_key=factor_key,
                factor=row["factor"],
                action="decrease_positive_weight",
                suggested_delta=-self._suggested_weight_delta(amount),
                evidence="rank_overvaluation",
                count=row.get("count", 0),
                average_ai_brier=row.get("average_ai_brier"),
                average_absolute_rank_error=row.get("average_absolute_rank_error"),
                statistical_note=row.get("statistical_note", "sample_small"),
                calibration=calibration_by_key.get(factor_key),
            )
        for row in factor_attribution_summary.get("overvalued_positive_factors") or []:
            factor_key = row.get("factor_key") or row["factor"]
            if factor_key in candidates:
                continue
            amount = abs(float(row.get("average_amount") or 0.0))
            candidates[factor_key] = self._factor_weight_candidate(
                factor_key=factor_key,
                factor=row["factor"],
                action="decrease_positive_weight",
                suggested_delta=-self._suggested_weight_delta(amount),
                evidence="overvalued_non_winners",
                count=row.get("count", 0),
                average_ai_brier=row.get("average_ai_brier"),
                statistical_note=row.get("statistical_note", "sample_small"),
                calibration=calibration_by_key.get(factor_key),
            )
        for row in factor_attribution_summary.get("rank_undervalued_negative_factors") or []:
            factor_key = row.get("factor_key") or row["factor"]
            amount = abs(float(row.get("average_amount") or 0.0))
            candidates[factor_key] = self._factor_weight_candidate(
                factor_key=factor_key,
                factor=row["factor"],
                action="weaken_negative_penalty",
                suggested_delta=self._suggested_weight_delta(amount),
                evidence="rank_undervaluation",
                count=row.get("count", 0),
                average_ai_brier=row.get("average_ai_brier"),
                average_absolute_rank_error=row.get("average_absolute_rank_error"),
                statistical_note=row.get("statistical_note", "sample_small"),
                calibration=calibration_by_key.get(factor_key),
            )
        for row in factor_attribution_summary.get("winner_negative_factors") or []:
            factor_key = row.get("factor_key") or row["factor"]
            if factor_key in candidates:
                continue
            amount = abs(float(row.get("average_amount") or 0.0))
            candidates[factor_key] = self._factor_weight_candidate(
                factor_key=factor_key,
                factor=row["factor"],
                action="weaken_negative_penalty",
                suggested_delta=self._suggested_weight_delta(amount),
                evidence="penalized_winners",
                count=row.get("count", 0),
                average_ai_brier=row.get("average_ai_brier"),
                statistical_note=row.get("statistical_note", "sample_small"),
                calibration=calibration_by_key.get(factor_key),
            )

        overvalued_factors = {
            row.get("factor_key") or row["factor"]
            for row in factor_attribution_summary.get("overvalued_positive_factors") or []
        } | {
            row.get("factor_key") or row["factor"]
            for row in factor_attribution_summary.get("rank_overvalued_positive_factors") or []
        }
        for row in factor_attribution_summary.get("winner_positive_factors") or []:
            factor_key = row.get("factor_key") or row["factor"]
            if factor_key in overvalued_factors or factor_key in candidates:
                continue
            amount = abs(float(row.get("average_amount") or 0.0))
            candidates[factor_key] = self._factor_weight_candidate(
                factor_key=factor_key,
                factor=row["factor"],
                action="monitor_or_slightly_increase",
                suggested_delta=self._suggested_weight_delta(amount) / 2,
                evidence="winner_positive_only",
                count=row.get("count", 0),
                average_ai_brier=row.get("average_ai_brier"),
                statistical_note=row.get("statistical_note", "sample_small"),
                calibration=calibration_by_key.get(factor_key),
            )

        return sorted(
            candidates.values(),
            key=lambda item: (
                item.get("average_absolute_rank_error") or 0.0,
                item.get("average_ai_brier") or 0.0,
                item.get("count") or 0,
            ),
            reverse=True,
        )[:10]

    def _calibration_by_factor_key(self, factor_calibration_summary: dict) -> dict[str, dict]:
        result: dict[str, dict] = {}
        for group in ("positive", "negative"):
            for row in factor_calibration_summary.get(group) or []:
                factor_key = row.get("factor_key")
                if factor_key and factor_key not in result:
                    result[factor_key] = row
        return result

    def _factor_weight_candidate(
        self,
        *,
        factor_key: str,
        factor: str,
        action: str,
        suggested_delta: float,
        evidence: str,
        count: int,
        average_ai_brier: float | None,
        statistical_note: str,
        calibration: dict | None,
        average_absolute_rank_error: float | None = None,
    ) -> dict:
        calibration_gap = calibration.get("calibration_gap") if calibration else None
        calibration_count = calibration.get("count") if calibration else 0
        readiness = self._factor_adjustment_readiness(count, calibration_gap, calibration_count)
        return {
            "factor_key": factor_key,
            "factor": factor,
            "action": action,
            "suggested_delta": suggested_delta,
            "evidence": evidence,
            "count": count,
            "average_ai_brier": average_ai_brier,
            "average_absolute_rank_error": average_absolute_rank_error,
            "calibration_gap": calibration_gap,
            "calibration_count": calibration_count,
            "readiness": readiness["readiness"],
            "deployment_status": "challenger",
            "sample_count_status": readiness["sample_count_status"],
            "gap_status": readiness["gap_status"],
            "statistical_note": statistical_note,
        }

    def _factor_adjustment_readiness(self, count: int, calibration_gap: float | None, calibration_count: int | None) -> dict:
        count = int(count or 0)
        calibration_count = int(calibration_count or 0)
        gap = abs(float(calibration_gap)) if calibration_gap is not None else None
        if count >= 30 and calibration_count >= 30:
            sample_count_status = "enough"
        elif count >= 10 and calibration_count >= 10:
            sample_count_status = "watch"
        else:
            sample_count_status = "low"

        if gap is None:
            gap_status = "unknown"
        elif gap >= 0.08:
            gap_status = "large"
        elif gap >= 0.04:
            gap_status = "medium"
        else:
            gap_status = "small"

        if sample_count_status == "enough" and gap_status in {"large", "medium"}:
            readiness = "candidate"
        elif sample_count_status in {"enough", "watch"} and gap_status in {"large", "medium"}:
            readiness = "watch"
        else:
            readiness = "monitor"
        return {
            "readiness": readiness,
            "sample_count_status": sample_count_status,
            "gap_status": gap_status,
        }

    def _suggested_weight_delta(self, average_amount: float) -> float:
        if average_amount <= 0:
            return 0.005
        return round(min(0.03, max(0.005, average_amount * 0.25)), 6)

    def _factor_attribution_summary(self, payloads: list[dict]) -> dict:
        return {
            "overvalued_positive_factors": self._aggregate_factor_rows(
                payloads,
                "factor_attribution",
                "overvalued_positive_factors",
            ),
            "winner_positive_factors": self._aggregate_factor_rows(
                payloads,
                "factor_attribution",
                "winner_positive_factors",
            ),
            "winner_negative_factors": self._aggregate_factor_rows(
                payloads,
                "factor_attribution",
                "winner_negative_factors",
            ),
            "rank_overvalued_positive_factors": self._aggregate_factor_rows(
                payloads,
                "factor_attribution",
                "rank_overvalued_positive_factors",
            ),
            "rank_undervalued_negative_factors": self._aggregate_factor_rows(
                payloads,
                "factor_attribution",
                "rank_undervalued_negative_factors",
            ),
            "note": "レビュー時点のscore_factorsから原因候補を集計します。件数が少ない間は自動重み変更ではなく確認材料です。",
        }

    def _aggregate_factor_rows(self, payloads: list[dict], group_key: str, row_key: str) -> list[dict]:
        buckets: dict[str, dict] = {}
        for payload in payloads:
            for row in ((payload.get(group_key) or {}).get(row_key) or []):
                factor = row.get("factor")
                if not factor:
                    continue
                factor_key = row.get("factor_key") or factor
                bucket = buckets.setdefault(
                    factor_key,
                    {
                        "factor_key": factor_key,
                        "factor": factor,
                        "count": 0,
                        "weighted_amount": 0.0,
                        "weighted_ai_probability": 0.0,
                        "weighted_ai_brier": 0.0,
                        "weighted_absolute_rank_error": 0.0,
                        "rank_error_count": 0,
                        "samples": [],
                    },
                )
                count = int(row.get("count") or 0)
                if count <= 0:
                    continue
                bucket["count"] += count
                bucket["weighted_amount"] += float(row.get("average_amount") or 0.0) * count
                bucket["weighted_ai_probability"] += float(row.get("average_ai_probability") or 0.0) * count
                bucket["weighted_ai_brier"] += float(row.get("average_ai_brier") or 0.0) * count
                if row.get("average_absolute_rank_error") is not None:
                    bucket["weighted_absolute_rank_error"] += float(row["average_absolute_rank_error"]) * count
                    bucket["rank_error_count"] += count
                for sample in row.get("samples") or []:
                    if len(bucket["samples"]) < 3:
                        bucket["samples"].append(sample)

        result = []
        for bucket in buckets.values():
            count = bucket["count"]
            if count <= 0:
                continue
            result.append(
                {
                    "factor_key": bucket["factor_key"],
                    "factor": bucket["factor"],
                    "count": count,
                    "average_amount": round(bucket["weighted_amount"] / count, 6),
                    "average_ai_probability": round(bucket["weighted_ai_probability"] / count, 6),
                    "average_ai_brier": round(bucket["weighted_ai_brier"] / count, 6),
                    "average_absolute_rank_error": (
                        round(bucket["weighted_absolute_rank_error"] / bucket["rank_error_count"], 6)
                        if bucket["rank_error_count"]
                        else None
                    ),
                    "samples": bucket["samples"],
                    "statistical_note": "sample_small" if count < 30 else "ok",
                }
            )
        return sorted(
            result,
            key=lambda item: (
                item.get("average_absolute_rank_error") or 0.0,
                item["average_ai_brier"],
                item["count"],
            ),
            reverse=True,
        )[:10]

    def _calibration_summary(self, payloads: list[dict]) -> dict:
        return {
            "ai": self._calibration_rows(
                [
                    {"probability": row.get("ai_probability"), "actual_win": row.get("actual_win")}
                    for payload in payloads
                    for row in payload.get("rows", [])
                ]
            ),
            "user": self._calibration_rows(
                [
                    {"probability": row.get("user_probability"), "actual_win": row.get("actual_win")}
                    for payload in payloads
                    for row in payload.get("rows", [])
                    if row.get("user_probability") is not None
                ]
            ),
            "note": "確率帯ごとの平均予測勝率と実際の勝率です。件数が少ない帯は参考値です。",
        }

    def _calibration_rows(self, rows: list[dict]) -> list[dict]:
        buckets: dict[str, list[dict]] = {}
        for row in rows:
            probability = row.get("probability")
            actual_win = row.get("actual_win")
            if probability is None or actual_win is None:
                continue
            bucket = self._probability_bucket(float(probability))
            buckets.setdefault(bucket, []).append({"probability": float(probability), "actual_win": float(actual_win)})
        result = []
        for bucket, values in sorted(buckets.items(), key=lambda item: item[0]):
            probabilities = [item["probability"] for item in values]
            actuals = [item["actual_win"] for item in values]
            result.append(
                {
                    "bucket": bucket,
                    "count": len(values),
                    "average_probability": round(mean(probabilities), 6),
                    "actual_win_rate": round(mean(actuals), 6),
                    "calibration_gap": round(mean(probabilities) - mean(actuals), 6),
                    "statistical_note": "sample_small" if len(values) < 30 else "ok",
                }
            )
        return result

    def _probability_bucket(self, probability: float) -> str:
        upper = min(100, max(10, int(probability * 100 // 10 + 1) * 10))
        lower = upper - 10
        return f"{lower:02d}-{upper:02d}%"

    def _comparison_summary(self, payloads: list[dict]) -> dict:
        counts = {"ai": 0, "user": 0, "tie": 0, "none": 0}
        deltas = []
        for payload in payloads:
            comparison = payload.get("comparison") or {}
            winner = comparison.get("winner") or "none"
            counts[winner if winner in counts else "none"] += 1
            if comparison.get("brier_delta") is not None:
                deltas.append(float(comparison["brier_delta"]))
        return {
            "ai_wins": counts["ai"],
            "user_wins": counts["user"],
            "ties": counts["tie"],
            "uncompared": counts["none"],
            "average_brier_delta": round(mean(deltas), 6) if deltas else None,
            "note": "Brier差は user_brier - ai_brier です。正ならAI優勢、負なら自分予想優勢です。",
        }

    def _condition_breakdown(self, reviews: list[PredictionReview]) -> list[dict]:
        buckets: dict[tuple[str, str], list[dict]] = {}
        for review in reviews:
            race = self.session.get(Race, review.race_id)
            conditions = self._race_conditions(race)
            for dimension, value in conditions.items():
                if value in (None, "", "unknown"):
                    continue
                buckets.setdefault((dimension, str(value)), []).append(review.payload)

        rows = []
        for (dimension, value), payloads in sorted(buckets.items()):
            ai_values = [payload.get("ai_brier_score") for payload in payloads if payload.get("ai_brier_score") is not None]
            user_values = [payload.get("user_brier_score") for payload in payloads if payload.get("user_brier_score") is not None]
            rows.append(
                {
                    "dimension": dimension,
                    "value": value,
                    "count": len(payloads),
                    "ai_brier_score": round(mean(ai_values), 6) if ai_values else None,
                    "user_brier_score": round(mean(user_values), 6) if user_values else None,
                    "statistical_note": "sample_small" if len(payloads) < 10 else "ok",
                }
            )
        return rows

    def _user_rating_breakdown(self, reviews: list[PredictionReview]) -> list[dict]:
        buckets: dict[tuple[str, str], list[float]] = {}
        for review in reviews:
            for row in review.payload.get("rows", []):
                user_brier = row.get("user_brier")
                ratings = row.get("user_ratings") or {}
                if user_brier is None:
                    continue
                for dimension, value in ratings.items():
                    if value is None:
                        continue
                    buckets.setdefault((dimension, str(value)), []).append(user_brier)

        return [
            {
                "dimension": dimension,
                "value": value,
                "count": len(values),
                "user_brier_score": round(mean(values), 6),
                "statistical_note": "sample_small" if len(values) < 10 else "ok",
            }
            for (dimension, value), values in sorted(buckets.items())
        ]

    def _horse_breakdown(self, reviews: list[PredictionReview]) -> list[dict]:
        buckets: dict[tuple[str, str], list[dict]] = {}
        for review in reviews:
            entries = {
                entry.horse_number: entry
                for entry in self.session.scalars(select(RaceEntry).where(RaceEntry.race_id == review.race_id)).all()
            }
            for row in review.payload.get("rows", []):
                entry = entries.get(row.get("horse_number"))
                dimensions = self._horse_dimensions(row, entry)
                for dimension, value in dimensions.items():
                    if value in (None, "", "unknown"):
                        continue
                    buckets.setdefault((dimension, str(value)), []).append(row)

        result = []
        for (dimension, value), rows in sorted(buckets.items()):
            ai_values = [row.get("ai_brier") for row in rows if row.get("ai_brier") is not None]
            user_values = [row.get("user_brier") for row in rows if row.get("user_brier") is not None]
            result.append(
                {
                    "dimension": dimension,
                    "value": value,
                    "count": len(rows),
                    "ai_brier_score": round(mean(ai_values), 6) if ai_values else None,
                    "user_brier_score": round(mean(user_values), 6) if user_values else None,
                    "statistical_note": "sample_small" if len(rows) < 30 else "ok",
                }
            )
        return result

    def _horse_dimensions(self, review_row: dict, entry: RaceEntry | None) -> dict:
        entry_payload = entry.payload if entry else {}
        age = self._field_value(entry_payload, "age")
        return {
            "popularity_band": self._popularity_band(review_row.get("popularity")),
            "frame_number": self._field_value(entry_payload, "frame_number"),
            "sex": self._field_value(entry_payload, "sex"),
            "age_band": self._age_band(age),
        }

    def _race_conditions(self, race: Race | None) -> dict:
        if race is None:
            return {
                "racecourse": "unknown",
                "surface": "unknown",
                "distance_band": "unknown",
                "track_condition": "unknown",
            }
        payload = race.payload or {}
        distance = self._field_value(payload, "distance")
        return {
            "racecourse": self._field_value(payload, "racecourse") or race.racecourse or "unknown",
            "surface": self._field_value(payload, "surface") or "unknown",
            "distance_band": self._distance_band(distance),
            "track_condition": self._field_value(payload, "track_condition") or "unknown",
        }

    def _field_value(self, payload: dict, key: str):
        value = payload.get(key)
        if isinstance(value, dict):
            return value.get("value")
        return value

    def _distance_band(self, distance) -> str:
        if distance is None:
            return "unknown"
        distance = int(distance)
        if distance <= 1400:
            return "short"
        if distance <= 1800:
            return "mile"
        if distance <= 2200:
            return "middle"
        return "long"

    def _popularity_band(self, popularity) -> str:
        if popularity is None:
            return "unknown"
        popularity = int(popularity)
        if popularity <= 3:
            return "favored_1_3"
        if popularity <= 6:
            return "mid_4_6"
        return "longshot_7_plus"

    def _age_band(self, age) -> str:
        if age is None:
            return "unknown"
        age = int(age)
        if age <= 3:
            return "age_3_or_less"
        if age <= 5:
            return "age_4_5"
        return "age_6_plus"
