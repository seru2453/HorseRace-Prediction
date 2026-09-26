from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from services.common.models import HorseFeature, RaceData, RaceEntryData
from services.evidence.collector import PredictionEvidence


JEV_QUALITATIVE_VERSION = "jev-qualitative-v2"
JEV_FEATURE_NAMES = (
    "jev_training_condition",
    "jev_training_improvement",
    "jev_bad_run_explainability",
    "jev_condition_change_positive",
    "jev_course_fit",
    "jev_surface_fit",
    "jev_rotation_fit",
    "jev_pace_fit",
    "jev_expert_sentiment",
    "jev_rebound_score",
)


@dataclass(frozen=True)
class JevConfig:
    api_key: str | None
    enabled: bool = False
    endpoint: str = "https://api.typesafe.ai/v1/systemone"
    model: str = "jev-latest"
    timeout_seconds: float = 10.0
    maximum_adjustment: float = 0.04
    minimum_confidence: float = 0.55

    @classmethod
    def from_env(cls) -> "JevConfig":
        api_key = os.getenv("TYPESAFE_API_KEY") or None
        enabled_value = os.getenv("JEV_ENABLED")
        enabled = bool(api_key) if enabled_value is None else enabled_value.strip().lower() in {"1", "true", "yes", "on"}
        return cls(
            api_key=api_key,
            enabled=enabled,
            endpoint=os.getenv("JEV_API_URL", cls.endpoint),
            model=os.getenv("JEV_MODEL", cls.model),
            timeout_seconds=float(os.getenv("JEV_TIMEOUT_SECONDS", "10")),
        )


Transport = Callable[[dict[str, Any]], dict[str, Any]]


class JevQualitativeEvaluator:
    def __init__(self, config: JevConfig | None = None, transport: Transport | None = None) -> None:
        self.config = config or JevConfig.from_env()
        self._transport = transport

    def apply(
        self,
        race: RaceData,
        entries: list[RaceEntryData],
        features: list[HorseFeature],
        evidence_by_horse: dict[str, list[PredictionEvidence]] | None = None,
    ) -> list[HorseFeature]:
        evidence_by_horse = evidence_by_horse or {}
        active = [feature for feature in features if not feature.features.get("scratched")]
        if not active:
            return features
        eligible = [feature for feature in active if evidence_by_horse.get(feature.horse_id)]
        status = "disabled" if not self.config.enabled or not self.config.api_key else "no_evidence"
        assessments = {
            feature.horse_id: self._empty_assessment(status, "")
            for feature in active
        }
        if not eligible or status == "disabled":
            return [self._with_assessment(feature, assessments.get(feature.horse_id)) for feature in features]

        state = self._build_state(race, entries, eligible, evidence_by_horse)
        input_hash = hashlib.sha256(
            json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        payload = {
            "state": state,
            "model": self.config.model,
            "questions": self._build_questions(eligible),
        }
        try:
            response = (self._transport or self._post)(payload)
            assessments.update(self._parse_assessments(eligible, response, input_hash))
        except (HTTPError, OSError, TimeoutError, ValueError, KeyError, TypeError):
            assessments.update({
                feature.horse_id: self._empty_assessment("error", input_hash)
                for feature in eligible
            })

        return [self._with_assessment(feature, assessments.get(feature.horse_id)) for feature in features]

    def _empty_assessment(self, status: str, input_hash: str) -> dict[str, Any]:
        return {
            "status": status,
            "version": JEV_QUALITATIVE_VERSION,
            "model": self.config.model,
            "input_hash": input_hash,
            "adjustment": 0.0,
            "confidence": None,
            "uncertainty": None,
            "structured_features": {name: None for name in JEV_FEATURE_NAMES},
        }

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            self.config.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        for attempt in range(2):
            try:
                with urlopen(request, timeout=self.config.timeout_seconds) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                if attempt == 0 and (exc.code in {429, 529} or exc.code >= 500):
                    time.sleep(0.25)
                    continue
                raise
        raise RuntimeError("Jev request failed")

    def _build_state(
        self,
        race: RaceData,
        entries: list[RaceEntryData],
        features: list[HorseFeature],
        evidence_by_horse: dict[str, list[PredictionEvidence]],
    ) -> dict[str, Any]:
        entries_by_horse = {entry.horse.horse_id: entry for entry in entries}
        horses: list[dict[str, Any]] = []
        for feature in features:
            entry = entries_by_horse[feature.horse_id]
            values = feature.features
            horses.append(
                {
                    "horse_number": feature.horse_number,
                    "horse_name": feature.horse_name,
                    "sex": entry.sex.value,
                    "age_band": values.get("age_band"),
                    "jockey": entry.jockey_name.value,
                    "trainer": entry.trainer_name.value,
                    "sire": entry.horse.sire.value,
                    "dam": entry.horse.dam.value,
                    "damsire": entry.horse.damsire.value,
                    "running_style": values.get("running_style"),
                    "recent_consistency": values.get("recent_consistency_label"),
                    "distance_change": values.get("distance_change"),
                    "surface_change": values.get("surface_change"),
                    "grade_change": self._grade_change_label(values.get("grade_step_from_last")),
                    "rotation": self._rotation_label(values.get("days_since_last_run")),
                    "body_weight_trend": self._weight_change_label(values.get("horse_weight_change")),
                    "course_frame_tendency": values.get("course_frame_bias_label"),
                    "track_bias_sample_status": values.get("track_bias_sample_status"),
                    "evidence": [
                        {
                            "source_type": evidence.source_type,
                            "source_name": evidence.source_name,
                            "published_at": evidence.published_at.isoformat() if evidence.published_at else None,
                            "author": evidence.author,
                            "text": evidence.normalized_text,
                            "confidence": evidence.confidence,
                            "content_hash": evidence.content_hash,
                        }
                        for evidence in evidence_by_horse.get(feature.horse_id, [])
                    ],
                }
            )
        return {
            "task": "Convert only the supplied pre-race evidence into structured features. Missing facts must remain unknown and outside knowledge is prohibited.",
            "race": {
                "name": race.race_name.value,
                "grade": race.grade.value,
                "class": race.race_class.value,
                "racecourse": race.racecourse.value,
                "surface": race.surface.value,
                "distance_band": self._distance_band(race.distance.value),
                "direction": race.direction.value,
                "weather": race.weather.value,
                "track_condition": race.track_condition.value,
                "field_pace": self._field_pace_label(features),
            },
            "horses": horses,
        }

    def _build_questions(self, features: list[HorseFeature]) -> dict[str, Any]:
        questions: dict[str, Any] = {}
        rubric = ["Strong negative", "Negative", "Neutral", "Positive", "Strong positive"]
        for feature in features:
            identifier = feature.horse_number
            subject = f"horse {feature.horse_number} ({feature.horse_name})"
            for name in JEV_FEATURE_NAMES:
                questions[f"{name}_{identifier}"] = {
                    "type": "score",
                    "instructions": f"Rate {name.removeprefix('jev_')} for {subject}. Use only explicit supplied evidence; return neutral when unsupported.",
                    "criteria": rubric,
                }
        return questions

    def _parse_assessments(
        self,
        features: list[HorseFeature],
        response: dict[str, Any],
        input_hash: str,
    ) -> dict[str, dict[str, Any]]:
        answers = response["answers"]
        model = str(response.get("model") or self.config.model)
        assessments: dict[str, dict[str, Any]] = {}
        for feature in features:
            structured: dict[str, float | None] = {}
            confidences: list[float] = []
            for name in JEV_FEATURE_NAMES:
                answer = answers[f"{name}_{feature.horse_number}"]
                score = max(0.0, min(4.0, float(answer["score"])))
                structured[name] = round((score - 2.0) / 2.0, 4)
                if answer.get("confidence") is not None:
                    confidences.append(max(0.0, min(1.0, float(answer["confidence"]))))
            confidence = sum(confidences) / len(confidences) if confidences else 0.0
            status = "structured" if confidence >= self.config.minimum_confidence else "low_confidence"
            assessments[feature.horse_id] = {
                "status": status,
                "version": JEV_QUALITATIVE_VERSION,
                "model": model,
                "input_hash": input_hash,
                "adjustment": 0.0,
                "confidence": round(confidence, 4),
                "uncertainty": round(1.0 - confidence, 4),
                "structured_features": structured,
            }
        return assessments

    def _with_assessment(self, feature: HorseFeature, assessment: dict[str, Any] | None) -> HorseFeature:
        if assessment is None:
            return feature
        values = dict(feature.features)
        values["jev_qualitative_adjustment"] = 0.0
        values["jev_qualitative"] = assessment
        values.update(assessment.get("structured_features") or {})
        values["jev_confidence"] = assessment.get("confidence")
        values["jev_uncertainty"] = assessment.get("uncertainty")
        return feature.model_copy(update={"features": values})

    def _distance_band(self, distance: Any) -> str | None:
        if distance is None:
            return None
        value = int(distance)
        if value <= 1400:
            return "sprint"
        if value <= 1800:
            return "mile"
        if value <= 2200:
            return "middle"
        return "staying"

    def _grade_change_label(self, value: Any) -> str | None:
        if value is None:
            return None
        step = int(value)
        if step >= 2:
            return "major_step_up"
        if step == 1:
            return "step_up"
        if step <= -1:
            return "step_down"
        return "same_level"

    def _rotation_label(self, value: Any) -> str | None:
        if value is None:
            return None
        days = int(value)
        if days < 14:
            return "short_turnaround"
        if days <= 70:
            return "standard"
        if days <= 120:
            return "fresh"
        if days < 180:
            return "long_break"
        return "very_long_break"

    def _weight_change_label(self, value: Any) -> str | None:
        if value is None:
            return None
        change = int(value)
        if change <= -10:
            return "large_decrease"
        if change >= 10:
            return "large_increase"
        return "stable_range"

    def _field_pace_label(self, features: list[HorseFeature]) -> str | None:
        pressure = next(
            (
                feature.features.get("field_early_pressure")
                for feature in features
                if feature.features.get("field_early_pressure") is not None
            ),
            None,
        )
        if pressure is None:
            return None
        if float(pressure) >= 0.55:
            return "high_early_pressure"
        if float(pressure) <= 0.35:
            return "low_early_pressure"
        return "balanced"
