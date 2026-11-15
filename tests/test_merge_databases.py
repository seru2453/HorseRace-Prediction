from datetime import datetime, timezone
import hashlib
import sqlite3

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from database.models import (
    ChallengerPrediction,
    Horse,
    ModelEvaluation,
    PredictionMetric,
    PredictionReview,
    PredictionSnapshot,
    Race,
    TrainingRun,
)
from database.repository import RaceRepository
from database.session import Base
from scripts.merge_databases import _review_exclusion_report, merge_databases


def _database(
    path,
    race_id,
    horse_id,
    horse_name,
    model_version="test-model",
    feature_version="mvp-feature-v32",
    prediction_timestamp=None,
    raw_features=None,
):
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            Race(
                race_id=race_id,
                race_date="2026-08-15",
                racecourse="札幌",
                race_number=1,
                payload={"surface": "芝", "race_class": "1勝クラス", "start_time": "10時00分", "distance": 1000},
            )
        )
        session.add(Horse(horse_id=horse_id, horse_name=horse_name, payload={"horse_id": horse_id}))
        session.add(
            PredictionSnapshot(
                id=1,
                race_id=race_id,
                horse_id=horse_id,
                horse_number=1,
                prediction_timestamp=prediction_timestamp or datetime(2026, 8, 15, 0, 0),
                model_version=model_version,
                feature_version=feature_version,
                simulation_seed=11,
                ai_win_probability=0.5,
                ai_top2_probability=0.7,
                ai_top3_probability=0.8,
                raw_features=raw_features or {"known_at_prediction": 1.0},
                prediction_payload={"horse_name": horse_name},
            )
        )
        session.flush()
        session.add(PredictionMetric(prediction_snapshot_id=1, base_score=0.5))
        session.add(
            ChallengerPrediction(
                prediction_snapshot_id=1,
                model_version="challenger",
                feature_version="mvp-feature-v32",
                target="actual_win",
                predicted_value=0.5,
                metadata_json={},
            )
        )
        trained_at = datetime(2026, 8, 16, tzinfo=timezone.utc)
        session.add(
            TrainingRun(
                id=1,
                model_version=model_version,
                trained_at=trained_at,
                status="completed",
                row_count=1,
                race_count=1,
                metrics_json={},
            )
        )
        session.add(
            ModelEvaluation(
                id=1,
                model_version=model_version,
                evaluation_period="2026-08-15..2026-08-15",
                sample_count=1,
                race_count=1,
                horse_count=1,
                training_run_id=1,
                metrics_json={},
            )
        )
        session.add(
            PredictionReview(
                id=1,
                race_id=race_id,
                payload={"rows": [{"horse_number": 1, "actual_win": 1.0, "finish_position": 1}]},
                created_at=datetime(2026, 8, 15, 2, 0),
            )
        )
        session.commit()
    engine.dispose()


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_merge_remaps_surrogate_keys_and_keeps_foreign_keys_and_snapshots(tmp_path):
    primary = tmp_path / "primary.sqlite3"
    secondary = tmp_path / "secondary.sqlite3"
    output = tmp_path / "merged.sqlite3"
    _database(primary, "race-a", "horse-a", "Primary")
    _database(secondary, "race-b", "horse-b", "Secondary", model_version="other-model")
    source_hashes = (_digest(primary), _digest(secondary))

    report = merge_databases(primary, secondary, output, tmp_path / "backups")

    assert (report["training_dataset"]["merged"]["training_races"], report["training_dataset"]["merged"]["training_rows"]) == (2, 2)
    assert report["merged_review_diagnostics"]["reviewed_races"] == 2
    assert report["merged_integrity"]["foreign_key_violations"] == []
    assert report["duplicate_natural_keys"] == {}
    assert report["duplicate_summary"]["post_merge_duplicate_natural_keys"] == 0
    assert len(report["backups"]) == 2
    assert (_digest(primary), _digest(secondary)) == source_hashes
    with sqlite3.connect(output) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("20260926_01",)
        snapshots = connection.execute(
            "SELECT id, race_id, raw_features FROM prediction_snapshots ORDER BY race_id"
        ).fetchall()
        assert [row[0] for row in snapshots] == [1, 2]
        assert all('"known_at_prediction": 1.0' in row[2] for row in snapshots)
        metrics = connection.execute(
            "SELECT prediction_snapshot_id FROM prediction_metrics ORDER BY prediction_snapshot_id"
        ).fetchall()
        challengers = connection.execute(
            "SELECT prediction_snapshot_id FROM challenger_predictions ORDER BY prediction_snapshot_id"
        ).fetchall()
        assert metrics == [(1,), (2,)]
        assert challengers == [(1,), (2,)]
        evaluations = connection.execute(
            "SELECT id, training_run_id FROM model_evaluations ORDER BY id"
        ).fetchall()
        assert evaluations == [(1, 1), (2, 2)]


def test_merge_dry_run_does_not_write_output_or_backups(tmp_path):
    primary = tmp_path / "primary.sqlite3"
    secondary = tmp_path / "secondary.sqlite3"
    output = tmp_path / "merged.sqlite3"
    _database(primary, "race-a", "horse-a", "Primary")
    _database(secondary, "race-b", "horse-b", "Secondary", model_version="other-model")
    source_hashes = (_digest(primary), _digest(secondary))

    report = merge_databases(primary, secondary, output, tmp_path / "backups", dry_run=True)

    assert report["output_path"] is None
    assert report["backups"] == []
    assert report["training_dataset"]["merged"]["training_rows"] == 2
    assert not output.exists()
    assert not (tmp_path / "backups").exists()
    assert (_digest(primary), _digest(secondary)) == source_hashes


def test_merge_refuses_to_overwrite_output(tmp_path):
    primary = tmp_path / "primary.sqlite3"
    secondary = tmp_path / "secondary.sqlite3"
    output = tmp_path / "existing.sqlite3"
    _database(primary, "race-a", "horse-a", "Primary")
    _database(secondary, "race-b", "horse-b", "Secondary", model_version="other-model")
    output.write_bytes(b"keep existing output")

    try:
        merge_databases(primary, secondary, output)
    except FileExistsError:
        pass
    else:
        raise AssertionError("merge should refuse to overwrite an existing output")
    assert output.read_bytes() == b"keep existing output"


def test_merge_report_excludes_post_start_predictions_without_rewriting_snapshots(tmp_path):
    primary = tmp_path / "primary.sqlite3"
    secondary = tmp_path / "secondary.sqlite3"
    output = tmp_path / "merged.sqlite3"
    _database(primary, "race-before", "horse-before", "Before")
    _database(
        secondary,
        "race-after",
        "horse-after",
        "After",
        prediction_timestamp=datetime(2026, 8, 15, 2, 0),
        raw_features={"post_start_snapshot_marker": 1.0},
    )

    report = merge_databases(primary, secondary, output, tmp_path / "backups")

    assert report["source_review_diagnostics"]["secondary"]["excluded_by_reason"] == {
        "invalid_prediction_time": 1
    }
    assert report["training_dataset"]["merged"]["training_races"] == 1
    assert "post_start_snapshot_marker" not in report["training_dataset"]["merged"]["feature_columns"]
    with sqlite3.connect(output) as connection:
        saved = connection.execute(
            "SELECT raw_features FROM prediction_snapshots WHERE race_id='race-after'"
        ).fetchone()[0]
    assert saved == '{"post_start_snapshot_marker": 1.0}'


def test_merge_keeps_mixed_feature_versions_missing_values_unknown(tmp_path):
    primary = tmp_path / "primary.sqlite3"
    secondary = tmp_path / "secondary.sqlite3"
    output = tmp_path / "merged.sqlite3"
    _database(
        primary,
        "race-new",
        "horse-new",
        "New",
        model_version="new-model",
        feature_version="mvp-feature-v33",
        raw_features={"new_only_feature": 12.0},
    )
    _database(
        secondary,
        "race-old",
        "horse-old",
        "Old",
        model_version="old-model",
        feature_version="mvp-feature-v32",
        raw_features={"old_only_feature": 3.0},
    )

    report = merge_databases(primary, secondary, output, tmp_path / "backups")

    assert report["training_dataset"]["merged"]["feature_versions"] == {
        "mvp-feature-v32": 1,
        "mvp-feature-v33": 1,
    }
    engine = create_engine(f"sqlite:///{output}")
    try:
        with Session(engine) as session:
            rows = RaceRepository(session).training_dataset()["rows"]
    finally:
        engine.dispose()

    rows_by_race = {row["race_id"]: row for row in rows}
    assert rows_by_race["race-new"]["features"] == {"new_only_feature": 12.0}
    assert rows_by_race["race-old"]["features"] == {"old_only_feature": 3.0}
    assert "new_only_feature" not in rows_by_race["race-old"]["features"]
    assert "old_only_feature" not in rows_by_race["race-new"]["features"]


def test_exclusion_report_classifies_unsupported_and_missing_snapshot(tmp_path):
    unsupported = tmp_path / "unsupported.sqlite3"
    missing_snapshot = tmp_path / "missing-snapshot.sqlite3"
    _database(unsupported, "race-unsupported", "horse-1", "Unsupported")
    _database(missing_snapshot, "race-no-snapshot", "horse-2", "No snapshot")

    engine = create_engine(f"sqlite:///{unsupported}")
    try:
        with Session(engine) as session:
            race = session.get(Race, "race-unsupported")
            race.payload = {**race.payload, "surface": "障害"}
            session.commit()
    finally:
        engine.dispose()
    with sqlite3.connect(missing_snapshot) as connection:
        connection.execute("DELETE FROM challenger_predictions")
        connection.execute("DELETE FROM prediction_metrics")
        connection.execute("DELETE FROM prediction_snapshots")

    unsupported_report = _review_exclusion_report(unsupported)
    missing_snapshot_report = _review_exclusion_report(missing_snapshot)

    assert unsupported_report["excluded_by_reason"] == {"unsupported_race_type": 1}
    assert unsupported_report["excluded"][0]["race_id"] == "race-unsupported"
    assert missing_snapshot_report["excluded_by_reason"] == {"missing_snapshot": 1}
    assert missing_snapshot_report["excluded"][0]["race_id"] == "race-no-snapshot"
