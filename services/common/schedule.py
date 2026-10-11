from __future__ import annotations

from datetime import date

from services.common.models import RaceScheduleItem


def validate_schedule_date(value: str | None) -> str:
    if not value:
        raise ValueError("date must use YYYY-MM-DD format.")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("date must use YYYY-MM-DD format.") from exc
    if parsed.isoformat() != value:
        raise ValueError("date must use YYYY-MM-DD format.")
    return value


def schedule_response(race_date: str, items: list[RaceScheduleItem]) -> dict:
    courses: dict[str, list[RaceScheduleItem]] = {}
    for item in sorted(items, key=lambda row: (row.start_time, row.racecourse, row.race_number)):
        courses.setdefault(item.racecourse, []).append(item)
    return {
        "race_date": race_date,
        "racecourses": [
            {
                "name": racecourse,
                "races": [
                    {
                        "race_number": item.race_number,
                        "start_time": item.start_time,
                        "race_type": item.race_type,
                    }
                    for item in sorted(races, key=lambda row: row.race_number)
                ],
            }
            for racecourse, races in courses.items()
        ],
    }
