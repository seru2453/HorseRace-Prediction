from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from typing import Any

import joblib

from services.ml.baseline import LOGISTIC_BASELINE_VERSION, TrainedLogisticBaseline


def artifact_path(label: str, artifact_dir: str | Path | None = None) -> Path:
    if label not in {"actual_win", "actual_top3"}:
        raise ValueError(f"Unsupported challenger label: {label}")
    directory = Path(artifact_dir or os.getenv("MODEL_ARTIFACT_DIR", "data/models"))
    return directory / f"{LOGISTIC_BASELINE_VERSION}-{label}.joblib"


def save_logistic_artifact(
    model: TrainedLogisticBaseline,
    label: str,
    feature_version: str | None,
    metrics: dict[str, Any],
    artifact_dir: str | Path | None = None,
) -> Path:
    path = artifact_path(label, artifact_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "model_version": LOGISTIC_BASELINE_VERSION,
        "label": label,
        "feature_version": feature_version,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "metrics": metrics,
        "model": model,
    }
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            temporary_path = Path(handle.name)
        joblib.dump(payload, temporary_path)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return path


def load_logistic_artifact(label: str, artifact_dir: str | Path | None = None) -> dict[str, Any] | None:
    path = artifact_path(label, artifact_dir)
    if not path.is_file():
        return None
    try:
        payload = joblib.load(path)
    except Exception:
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("model_version") != LOGISTIC_BASELINE_VERSION
        or payload.get("label") != label
        or not isinstance(payload.get("model"), TrainedLogisticBaseline)
    ):
        return None
    return payload
