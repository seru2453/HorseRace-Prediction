from __future__ import annotations

import json
import os

from database.repository import RaceRepository
from database.session import get_session_factory
from services.prediction.stability_evaluation import evaluate_score_stability


def main() -> None:
    with get_session_factory(os.getenv("DATABASE_URL"))() as session:
        rows = RaceRepository(session).training_dataset()["rows"]
    print(json.dumps(evaluate_score_stability(rows), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
