from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from database.repository import RaceRepository
from database.session import get_session_factory
from services.app.orchestrator import AnalysisRequest, RaceAnalysisService
from services.data_provider.base import ProviderUnavailableError, RacingDataProvider
from services.learning.auto_review import AutomaticReviewService


JST = ZoneInfo("Asia/Tokyo")


class DailyRacePipeline:
    def __init__(self, provider: RacingDataProvider, database_url: str | None = None) -> None:
        self.provider = provider
        self.database_url = database_url
        self.session_factory = get_session_factory(database_url)

    def run_once(
        self,
        now: datetime | None = None,
        prediction_lead_minutes: int = 120,
        max_predictions: int = 12,
    ) -> dict:
        current = (now or datetime.now(JST)).astimezone(JST)
        race_date = current.date().isoformat()
        discovery_error: str | None = None
        try:
            schedule = self.provider.list_races(race_date)
        except ProviderUnavailableError as exc:
            schedule = []
            discovery_error = str(exc)
        outcomes: list[dict] = []
        predicted = 0

        for item in schedule:
            race_id = f"{item.race_date.replace('-', '')}-{item.racecourse}-{item.race_number}"
            if item.race_type != "flat":
                outcomes.append({"race_id": race_id, "status": "unsupported_race_type"})
                continue
            with self.session_factory() as session:
                predicted_race_id = RaceRepository(session).predicted_race_id(
                    item.race_date, item.racecourse, item.race_number
                )
                if predicted_race_id:
                    outcomes.append({"race_id": predicted_race_id, "status": "already_predicted"})
                    continue

            start = datetime.fromisoformat(f"{item.race_date}T{item.start_time}").replace(tzinfo=JST)
            if current >= start:
                outcomes.append({"race_id": race_id, "status": "missed_prediction_window"})
                continue
            if current < start - timedelta(minutes=max(1, prediction_lead_minutes)):
                outcomes.append({"race_id": race_id, "status": "scheduled"})
                continue
            if predicted >= max(1, max_predictions):
                outcomes.append({"race_id": race_id, "status": "deferred"})
                continue

            try:
                RaceAnalysisService(self.provider, self.database_url).analyze(
                    AnalysisRequest(
                        item.race_date,
                        item.racecourse,
                        item.race_number,
                        enforce_pre_race=True,
                        execution_time=current,
                    )
                )
            except (ProviderUnavailableError, ValueError) as exc:
                outcomes.append({"race_id": race_id, "status": "prediction_pending", "message": str(exc)})
                continue
            outcomes.append({"race_id": race_id, "status": "predicted"})
            predicted += 1

        reviews = AutomaticReviewService(self.provider, self.database_url).run_once(limit=200)
        prediction_statuses = [item["status"] for item in outcomes]
        review_statuses = [item["status"] for item in reviews]
        summary = {
            "schedule_count": len(schedule),
            "expected_flat_races": sum(item.race_type == "flat" for item in schedule),
            "predicted": prediction_statuses.count("predicted"),
            "predicted_races": prediction_statuses.count("predicted") + prediction_statuses.count("already_predicted"),
            "already_predicted": prediction_statuses.count("already_predicted"),
            "missed_prediction_window": prediction_statuses.count("missed_prediction_window"),
            "missed_prediction_races": prediction_statuses.count("missed_prediction_window"),
            "prediction_pending": sum(status in {"prediction_pending", "deferred"} for status in prediction_statuses),
            "reviewed": review_statuses.count("reviewed"),
            "results_saved": sum(item.get("result_saved", False) for item in reviews),
            "reviews_saved": sum(item.get("review_saved", False) for item in reviews),
            "result_saved_races": sum(item.get("result_saved", False) for item in reviews),
            "review_saved_races": sum(item.get("review_saved", False) for item in reviews),
            "result_pending": review_statuses.count("pending"),
            "errors": int(discovery_error is not None)
            + prediction_statuses.count("missed_prediction_window")
            + prediction_statuses.count("error")
            + review_statuses.count("error"),
        }
        return {
            "race_date": race_date,
            **summary,
            "discovery_error": discovery_error,
            "predictions": outcomes,
            "reviews": reviews,
        }


def start_daily_pipeline_worker(provider: RacingDataProvider, database_url: str | None = None) -> threading.Thread:
    interval = max(1800.0, float(os.getenv("AUTO_PIPELINE_INTERVAL_SECONDS", "1800")))
    initial_delay = max(5.0, float(os.getenv("AUTO_PIPELINE_INITIAL_DELAY_SECONDS", "30")))
    lead_minutes = max(30, int(os.getenv("AUTO_PREDICTION_LEAD_MINUTES", "120")))
    service = DailyRacePipeline(provider, database_url)
    stop_event = threading.Event()

    def run() -> None:
        if stop_event.wait(initial_delay):
            return
        while True:
            try:
                result = service.run_once(prediction_lead_minutes=lead_minutes)
                print(
                    "[daily-pipeline] "
                    + " ".join(
                        f"{key}={result[key]}"
                        for key in (
                            "schedule_count", "predicted", "already_predicted", "prediction_pending",
                            "reviewed", "result_pending", "errors",
                        )
                    ),
                    flush=True,
                )
            except Exception as exc:
                print(f"[daily-pipeline] failed: {exc}", flush=True)
            if stop_event.wait(interval):
                return

    thread = threading.Thread(target=run, name="daily-race-pipeline", daemon=True)
    thread.stop_event = stop_event  # type: ignore[attr-defined]
    thread.start()
    return thread
