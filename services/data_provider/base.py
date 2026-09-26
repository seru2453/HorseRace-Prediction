from __future__ import annotations

from abc import ABC, abstractmethod

from services.common.models import HorseData, RaceData, RaceEntryData, RaceResultData, RaceScheduleItem


class ProviderUnavailableError(RuntimeError):
    pass


class RacingDataProvider(ABC):
    def list_races(self, race_date: str) -> list[RaceScheduleItem]:
        raise ProviderUnavailableError("This provider does not support race schedule discovery.")

    @abstractmethod
    def get_race(self, race_date: str, racecourse: str, race_number: int) -> RaceData:
        raise NotImplementedError

    @abstractmethod
    def get_race_entries(self, race_id: str) -> list[RaceEntryData]:
        raise NotImplementedError

    @abstractmethod
    def get_horse_history(self, horse: HorseData) -> list:
        raise NotImplementedError

    @abstractmethod
    def get_race_results(self, race_id: str) -> RaceResultData | None:
        raise NotImplementedError

    @abstractmethod
    def get_race_updates(self, race_id: str) -> dict:
        raise NotImplementedError
