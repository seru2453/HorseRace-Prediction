"""Leakage-safe long-term race-series statistics from already stored results."""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from typing import Any


RACE_HISTORY_LOOKBACK_YEARS = 10
RACE_HISTORY_PRIOR_STRENGTH = 20.0


def normalize_race_name(value: object) -> str | None:
    if value in (None, ""):
        return None
    normalized = unicodedata.normalize("NFKC", str(value)).casefold()
    normalized = re.sub(r"[\s　・･()（）「」『』【】\[\]［］._‐‑‒–—―-]+", "", normalized)
    return normalized or None


def race_series_key(race: dict[str, Any]) -> tuple[str, str, str, str | None, str | None] | None:
    name = normalize_race_name(race.get("race_name"))
    course = _text(race.get("racecourse"))
    surface = _text(race.get("surface"))
    distance = _text(race.get("distance"))
    if not all((name, course, surface, distance)):
        return None
    return (name, course, surface, distance, _text(race.get("grade") or race.get("race_class")))


def _text(value: object) -> str | None:
    if value in (None, ""):
        return None
    return unicodedata.normalize("NFKC", str(value)).strip().casefold()


def race_history_features(
    target_race: dict[str, Any],
    entries: list[dict[str, Any]],
    historical_races: list[dict[str, Any]],
    prior_strength: float = RACE_HISTORY_PRIOR_STRENGTH,
) -> dict[str, dict[str, Any]]:
    """Return shrunk frame/age/popularity top3 rates; absent evidence remains None."""
    series_key = race_series_key(target_race)
    course_rows: dict[tuple[str, str], list[bool]] = defaultdict(list)
    series_rows: dict[tuple[str, str], list[bool]] = defaultdict(list)
    for historical in historical_races:
        same_course = (
            _text(historical.get("racecourse")),
            _text(historical.get("surface")),
            _text(historical.get("distance")),
        ) == (
            _text(target_race.get("racecourse")),
            _text(target_race.get("surface")),
            _text(target_race.get("distance")),
        )
        if not same_course:
            continue
        same_series = series_key is not None and race_series_key(historical) == series_key
        for historical_entry in historical.get("entries", []):
            finish = historical_entry.get("finish_position")
            if finish in (None, ""):
                continue
            top3 = int(finish) <= 3
            for kind, category in _categories(historical_entry).items():
                key = (kind, category)
                course_rows[key].append(top3)
                if same_series:
                    series_rows[key].append(top3)

    result: dict[str, dict[str, Any]] = {}
    for entry in entries:
        horse_id = str(entry.get("horse_id") or "")
        values = {}
        for kind, category in _categories(entry).items():
            key = (kind, category)
            course = course_rows.get(key, [])
            series = series_rows.get(key, [])
            course_rate = sum(course) / len(course) if course else None
            series_rate = sum(series) / len(series) if series else None
            if series and course_rate is not None:
                shrunk = (sum(series) + max(0.0, prior_strength) * course_rate) / (len(series) + max(0.0, prior_strength))
            elif course_rate is not None:
                shrunk = course_rate
            else:
                shrunk = None
            values[f"race_history_{kind}_top3_rate"] = shrunk
            values[f"race_history_{kind}_specific_top3_rate"] = series_rate
            values[f"race_history_{kind}_sample_count"] = len(series)
            values[f"race_history_{kind}_course_sample_count"] = len(course)
            values[f"race_history_{kind}_sample_status"] = _sample_status(len(series))
        result[horse_id] = values
    return result


def _categories(entry: dict[str, Any]) -> dict[str, str]:
    result = {}
    frame = _number(entry.get("frame_number"))
    age = _number(entry.get("age"))
    popularity = _number(entry.get("popularity"))
    if frame is not None:
        result["frame"] = "inner" if frame <= 3 else "middle" if frame <= 5 else "outside"
    if age is not None:
        result["age"] = "3" if age == 3 else "4" if age == 4 else "5" if age == 5 else "6_plus"
    if popularity is not None:
        result["popularity"] = "1" if popularity == 1 else "2_3" if popularity <= 3 else "4_6" if popularity <= 6 else "7_plus"
    return result


def _number(value: object) -> int | None:
    if isinstance(value, dict):
        value = value.get("value")
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _sample_status(count: int) -> str:
    if count == 0:
        return "UNKNOWN"
    if count < 3:
        return "VERY_LOW"
    if count < 6:
        return "LOW_SAMPLE"
    return "WATCH"
