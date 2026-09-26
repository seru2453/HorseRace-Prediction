from __future__ import annotations

import json
from pathlib import Path

from services.common.models import (
    HorseData,
    RaceData,
    RaceEntryData,
    RaceResultData,
    RaceScheduleItem,
)
from services.data_provider.base import RacingDataProvider


class MockProvider(RacingDataProvider):
    def __init__(self, fixture_path: Path | None = None) -> None:
        self.fixture_path = fixture_path or Path(__file__).resolve().parents[2] / "fixtures" / "demo_race.json"
        with self.fixture_path.open(encoding="utf-8") as f:
            self.payload = json.load(f)

    def get_race(self, race_date: str, racecourse: str, race_number: int) -> RaceData:
        race = RaceData.model_validate(self.payload["race"])
        if (
            race.race_date.value != race_date
            or race.racecourse.value != racecourse
            or int(race.race_number.value) != int(race_number)
        ):
            raise ValueError("Demo fixture contains only 2026-08-16 札幌 11R.")
        return race

    def list_races(self, race_date: str) -> list[RaceScheduleItem]:
        race = RaceData.model_validate(self.payload["race"])
        if str(race.race_date.value) != race_date:
            return []
        return [
            RaceScheduleItem(
                race_date=race_date,
                racecourse=str(race.racecourse.value),
                race_number=int(race.race_number.value),
                start_time=str(race.start_time.value),
            )
        ]

    def get_race_entries(self, race_id: str) -> list[RaceEntryData]:
        return [RaceEntryData.model_validate(item) for item in self.payload["entries"] if item["race_id"] == race_id]

    def get_horse_history(self, horse: HorseData) -> list:
        return self.payload.get("history", {}).get(horse.horse_id, [])

    def get_race_results(self, race_id: str) -> RaceResultData | None:
        result = self.payload.get("result")
        if result and result["race_id"] == race_id:
            return RaceResultData.model_validate(result)
        return None

    def get_race_updates(self, race_id: str) -> dict:
        return {"race_id": race_id, "source": "fixture", "has_late_changes": False}
