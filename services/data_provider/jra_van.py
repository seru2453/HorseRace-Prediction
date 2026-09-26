from __future__ import annotations

from services.common.models import HorseData, RaceData, RaceEntryData, RaceResultData
from services.data_provider.base import ProviderUnavailableError, RacingDataProvider


class JraVanProvider(RacingDataProvider):
    def get_race(self, race_date: str, racecourse: str, race_number: int) -> RaceData:
        raise ProviderUnavailableError("JRA-VAN Data Lab. integration is intentionally not implemented in the MVP.")

    def get_race_entries(self, race_id: str) -> list[RaceEntryData]:
        raise ProviderUnavailableError("JRA-VAN Data Lab. integration is intentionally not implemented in the MVP.")

    def get_horse_history(self, horse: HorseData) -> list:
        raise ProviderUnavailableError("JRA-VAN Data Lab. integration is intentionally not implemented in the MVP.")

    def get_race_results(self, race_id: str) -> RaceResultData | None:
        raise ProviderUnavailableError("JRA-VAN Data Lab. integration is intentionally not implemented in the MVP.")

    def get_race_updates(self, race_id: str) -> dict:
        raise ProviderUnavailableError("JRA-VAN Data Lab. integration is intentionally not implemented in the MVP.")
