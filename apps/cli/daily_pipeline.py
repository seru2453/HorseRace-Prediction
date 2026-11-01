from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.data_provider.jra_official import JraOfficialProvider
from services.learning.daily_pipeline import DailyRacePipeline
from services.learning.auto_review import AutomaticReviewService


def main() -> int:
    parser = argparse.ArgumentParser(description="Discover, predict and review today's JRA races.")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=int, default=1800)
    parser.add_argument("--results-only", action="store_true", help="Retry results/reviews for saved prediction snapshots only")
    args = parser.parse_args()
    provider = JraOfficialProvider()
    if args.results_only:
        outcomes = AutomaticReviewService(provider, os.getenv("DATABASE_URL")).run_once(limit=200)
        print(
            json.dumps(
                {
                    "mode": "results_only",
                    "reviewed": sum(item.get("review_saved", False) for item in outcomes),
                    "result_saved_races": sum(item.get("result_saved", False) for item in outcomes),
                    "review_saved_races": sum(item.get("review_saved", False) for item in outcomes),
                    "pending": sum(item.get("status") == "pending" for item in outcomes),
                    "errors": sum(item.get("status") == "error" for item in outcomes),
                    "outcomes": outcomes,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    service = DailyRacePipeline(provider, os.getenv("DATABASE_URL"))
    while True:
        print(json.dumps(service.run_once(), ensure_ascii=False, indent=2))
        if not args.watch:
            return 0
        time.sleep(max(1800, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())
