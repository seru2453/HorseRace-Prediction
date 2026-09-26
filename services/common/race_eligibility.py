from __future__ import annotations

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


JST = ZoneInfo("Asia/Tokyo")


def is_supported_flat_race(surface: object, race_class: object = None) -> bool:
    return str(surface or "") in {"芝", "ダート"} and "障害" not in str(race_class or "")


def race_start_datetime(race_date: object, start_time: object) -> datetime | None:
    if not race_date or not start_time:
        return None
    match = re.search(r"(\d{1,2})(?::|時)(\d{2})(?:分)?", str(start_time))
    if not match:
        return None
    try:
        day = datetime.fromisoformat(str(race_date)).date()
    except ValueError:
        return None
    try:
        return datetime(day.year, day.month, day.day, int(match.group(1)), int(match.group(2)), tzinfo=JST)
    except ValueError:
        return None


def prediction_lead_minutes(race_date: object, start_time: object, predicted_at: datetime | None) -> float | None:
    start = race_start_datetime(race_date, start_time)
    if start is None or predicted_at is None:
        return None
    timestamp = predicted_at if predicted_at.tzinfo else predicted_at.replace(tzinfo=timezone.utc)
    return (start - timestamp.astimezone(JST)).total_seconds() / 60.0
