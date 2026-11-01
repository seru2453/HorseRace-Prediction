from __future__ import annotations

import os
import threading
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from database.repository import RaceRepository
from database.session import get_session_factory
from services.data_provider.base import ProviderUnavailableError, RacingDataProvider
from services.learning.review import PredictionReviewer


class AutomaticReviewService:
    def __init__(self, provider: RacingDataProvider, database_url: str | None = None) -> None:
        self.provider = provider
        self.session_factory = get_session_factory(database_url)

    def run_once(self, limit: int = 50, save: bool = True) -> list[dict]:
        with self.session_factory() as session:
            repo = RaceRepository(session)
            unreviewed = repo.race_history(limit=limit, has_review=False)
            missing_results = repo.race_history(limit=limit, has_result=False)
            rows_by_race = {row["race_id"]: row for row in unreviewed}
            rows_by_race.update({row["race_id"]: row for row in missing_results})
            rows = list(rows_by_race.values())

        outcomes = []
        today = date.today().isoformat()
        for row in rows:
            race_id = row["race_id"]
            if row.get("race_date") and row["race_date"] > today:
                outcomes.append({"race_id": race_id, "status": "skipped", "message": "未来日のレース"})
                continue
            if not self._result_is_due(row):
                outcomes.append({"race_id": race_id, "status": "skipped", "message": "発走前または結果確定待ち"})
                continue

            with self.session_factory() as session:
                repo = RaceRepository(session)
                predictions = repo.latest_snapshot_predictions(race_id)
                user_predictions = repo.latest_user_predictions(race_id)
                race_result, saved_source = repo.saved_result(race_id)
            if not predictions:
                outcomes.append({"race_id": race_id, "status": "skipped", "message": "保存済みAI予測なし"})
                continue

            result_was_saved = race_result is not None
            review_already_saved = bool(row.get("review_count"))
            result_source = f"saved_{saved_source}" if race_result else "provider"
            if race_result is None:
                try:
                    race_result = self.provider.get_race_results(race_id)
                except (ProviderUnavailableError, ValueError) as exc:
                    outcomes.append({"race_id": race_id, "status": "pending", "message": str(exc)})
                    continue
            if race_result is None:
                outcomes.append({"race_id": race_id, "status": "pending", "message": "結果未取得"})
                continue

            review = None
            if not review_already_saved:
                review = PredictionReviewer().review(predictions, race_result, user_predictions)
                review["prediction_source"] = "saved_snapshot"
                review["user_prediction_source"] = "saved" if user_predictions else "none"
                review["result_source"] = result_source
            actual_top3_numbers = {
                item.horse_number
                for item in race_result.entries
                if item.finish_position is not None and item.finish_position <= 3
            }
            if save:
                with self.session_factory() as session:
                    repo = RaceRepository(session)
                    if not result_was_saved:
                        repo.save_result(race_result)
                    if review is not None:
                        repo.save_review(race_id, review)
            outcomes.append(
                {
                    "race_id": race_id,
                    "status": "result_saved" if review_already_saved else "reviewed",
                    "result_source": result_source,
                    "result_saved": bool(save and not result_was_saved),
                    "review_saved": bool(save and review is not None),
                    "ai_brier_score": review.get("ai_brier_score") if review else None,
                    "top3_hit_count": len({item.horse_number for item in predictions[:3]} & actual_top3_numbers),
                }
            )
        return outcomes

    def _result_is_due(self, row: dict) -> bool:
        race_date = row.get("race_date")
        start_time = row.get("start_time")
        if not race_date or not start_time:
            return True
        try:
            start = datetime.fromisoformat(f"{race_date}T{start_time}").replace(tzinfo=ZoneInfo("Asia/Tokyo"))
        except ValueError:
            return True
        return datetime.now(ZoneInfo("Asia/Tokyo")) >= start + timedelta(minutes=15)


def start_auto_review_worker(provider: RacingDataProvider, database_url: str | None = None) -> threading.Thread:
    interval = max(300.0, float(os.getenv("AUTO_REVIEW_INTERVAL_SECONDS", "3600")))
    initial_delay = max(5.0, float(os.getenv("AUTO_REVIEW_INITIAL_DELAY_SECONDS", "30")))
    service = AutomaticReviewService(provider, database_url)

    def run() -> None:
        if stop_event.wait(initial_delay):
            return
        while True:
            try:
                outcomes = service.run_once()
                reviewed = len([item for item in outcomes if item["status"] == "reviewed"])
                if reviewed:
                    print(f"[auto-review] reviewed={reviewed}", flush=True)
            except Exception as exc:
                print(f"[auto-review] failed: {exc}", flush=True)
            if stop_event.wait(interval):
                return

    stop_event = threading.Event()
    thread = threading.Thread(target=run, name="automatic-race-review", daemon=True)
    thread.stop_event = stop_event  # type: ignore[attr-defined]
    thread.start()
    return thread
