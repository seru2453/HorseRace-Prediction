"""Create a weekly, shadow-only logistic challenger from saved prediction data."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from database.models import TrainingRun
from database.repository import RaceRepository
from database.session import make_session_factory
from services.ml.artifacts import save_logistic_artifact
from services.ml.baseline import (
    LOGISTIC_BASELINE_VERSION,
    LogisticBaselineTrainer,
    TrainingConfig,
    evaluate_challenger_dataset,
)


def train_weekly(now: datetime | None = None, database_url: str | None = None, artifact_root: Path | None = None) -> dict:
    local_now = now or datetime.now(ZoneInfo("Asia/Tokyo"))
    week_key = local_now.date().isocalendar()
    scheduled_week = f"{week_key.year}-W{week_key.week:02d}"
    repo_key = hashlib.sha256(str(ROOT).encode()).hexdigest()[:16]
    lock_path = Path(tempfile.gettempdir()) / f"horseprediction-weekly-training-{repo_key}.lock"
    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        session_factory = make_session_factory(database_url)
        with session_factory() as session:
            existing = session.scalars(select(TrainingRun)).all()
            for run in existing:
                if (run.metrics_json or {}).get("scheduled_week") == scheduled_week:
                    return {"status": "already_trained", "scheduled_week": scheduled_week, "training_run_id": run.id}
            repository = RaceRepository(session)
            pending = [
                row["race_id"]
                for row in repository.race_history(limit=10000)
                if row.get("race_date") and row["race_date"] <= local_now.date().isoformat()
                and (not row.get("has_result") or not row.get("review_count"))
                and repository.latest_snapshot_predictions(row["race_id"])
            ]
            if pending:
                return {"status": "deferred_pending_results", "scheduled_week": scheduled_week, "pending_race_count": len(pending)}
            dataset = repository.training_dataset()

        trainer = LogisticBaselineTrainer(TrainingConfig(label="actual_win"))
        metrics, model = trainer.fit(dataset)
        evaluation_with_jev = evaluate_challenger_dataset(dataset, include_jev=True)
        evaluation_without_jev = evaluate_challenger_dataset(dataset, include_jev=False)
        metrics["temporal_evaluation"] = evaluation_with_jev
        metrics["jev_ablation"] = {"with_jev": evaluation_with_jev, "without_jev": evaluation_without_jev}
        metrics["scheduled_week"] = scheduled_week
        metrics["challenger_only"] = True
        metrics["promotion_status"] = "not_promoted"
        if int(dataset.get("race_count") or 0) < 100:
            metrics["sample_status"] = "sample_small"
            metrics["application_status"] = "reference_only"
        feature_versions = sorted({str(row["feature_version"]) for row in dataset.get("rows", []) if row.get("feature_version")})
        feature_version = feature_versions[0] if len(feature_versions) == 1 else "mixed" if feature_versions else None
        if model is not None and metrics.get("status") == "trained_in_sample":
            configured_artifact_root = artifact_root or Path(os.getenv("MODEL_ARTIFACT_DIR", ROOT / "data" / "models"))
            artifact_dir = configured_artifact_root / "weekly" / scheduled_week
            metrics["model_artifact"] = str(save_logistic_artifact(model, "actual_win", feature_version, metrics, artifact_dir))
        else:
            metrics["model_artifact"] = None
        with session_factory() as session:
            repository = RaceRepository(session)
            run = repository.save_training_run(LOGISTIC_BASELINE_VERSION, feature_version, dataset, metrics)
            repository.save_model_evaluation(LOGISTIC_BASELINE_VERSION, feature_version, evaluation_with_jev, run.id)
            repository.save_model_evaluation(LOGISTIC_BASELINE_VERSION, feature_version, evaluation_without_jev, run.id)
        return {**metrics, "training_run_id": run.id}


if __name__ == "__main__":
    try:
        print(json.dumps(train_weekly(database_url=os.getenv("DATABASE_URL")), ensure_ascii=False, indent=2))
    except BlockingIOError:
        print(json.dumps({"status": "already_running"}, ensure_ascii=False))
