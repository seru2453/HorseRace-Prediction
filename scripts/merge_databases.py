"""Safely merge two HorseRace SQLite databases into a new candidate database.

The source databases are only opened read-only. Backups are made with SQLite's
online backup API before a non-dry-run merge. Conflicting natural keys keep the
primary row; surrogate integer primary keys are remapped and their foreign keys
follow the remap. Historical prediction snapshots are copied verbatim.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import uuid
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.models import (  # noqa: E402
    PredictionReview,
    PredictionSnapshot,
    Race,
    RaceResult,
    RaceResultEntry,
)
from database.repository import RaceRepository  # noqa: E402
from database.session import Base  # noqa: E402
from services.common.race_eligibility import (  # noqa: E402
    is_supported_flat_race,
    race_start_datetime,
)


NATURAL_KEYS: dict[str, tuple[tuple[str, ...], ...]] = {
    "races": (("race_id",),),
    "horses": (("horse_id",),),
    "race_entries": (("race_id", "horse_number"),),
    "past_performances": (("horse_id", "race_date", "racecourse", "race_name"),),
    "predictions": (("race_id", "horse_id", "model_version", "feature_version", "prediction_timestamp"),),
    "prediction_snapshots": (("race_id", "horse_id", "horse_number", "prediction_timestamp", "simulation_seed"),),
    "prediction_metrics": (("prediction_snapshot_id",),),
    "challenger_predictions": (("prediction_snapshot_id", "model_version", "target"),),
    "analysis_runs": (("race_id", "provider_name", "created_at"),),
    "simulations": (("race_id", "iterations", "seed", "created_at"),),
    "user_predictions": (("race_id", "horse_number", "prediction_timestamp"),),
    "race_results": (("race_id",),),
    "race_result_entries": (("race_id", "horse_number"),),
    "prediction_reviews": (("race_id", "created_at"),),
    "qualitative_evidence": (("content_hash", "race_id", "horse_id"),),
    "source_reliability": (("source_type", "source_name"),),
    "training_runs": (("model_version", "trained_at"),),
    "model_evaluations": (("model_version", "evaluated_at"),),
}
CURRENT_ALEMBIC_REVISION = "20260926_01"


class MergeError(RuntimeError):
    """Raised when a safe merge cannot be completed."""


def _readonly_connection(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(f"SQLite database does not exist: {path}")
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _table_names(connection: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def _schema_snapshot(connection: sqlite3.Connection) -> dict[str, Any]:
    tables: dict[str, Any] = {}
    for table in _table_names(connection):
        columns = [dict(row) for row in connection.execute(f'PRAGMA table_info("{table}")')]
        indexes = []
        for index in connection.execute(f'PRAGMA index_list("{table}")'):
            names = [
                row[2]
                for row in connection.execute(f'PRAGMA index_info("{index[1]}")')
                if row[2] is not None
            ]
            indexes.append(
                {
                    "unique": bool(index[2]),
                    "columns": names,
                    "origin": index[3],
                    "partial": bool(index[4]),
                }
            )
        foreign_keys: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in connection.execute(f'PRAGMA foreign_key_list("{table}")'):
            foreign_keys[row[0]].append(
                {
                    "table": row[2],
                    "from": row[3],
                    "to": row[4],
                    "sequence": row[1],
                    "on_update": row[5],
                    "on_delete": row[6],
                    "match": row[7],
                }
            )
        tables[table] = {
            "columns": columns,
            "indexes": indexes,
            "foreign_keys": [part for group in foreign_keys.values() for part in group],
        }
    return tables


def _counts(connection: sqlite3.Connection, tables: list[str] | None = None) -> dict[str, int]:
    selected = tables or _table_names(connection)
    result = {}
    available = set(_table_names(connection))
    for table in selected:
        if table in available:
            result[table] = int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    return result


def _integrity(connection: sqlite3.Connection) -> dict[str, Any]:
    return {
        "integrity_check": [row[0] for row in connection.execute("PRAGMA integrity_check")],
        "foreign_key_violations": [list(row) for row in connection.execute("PRAGMA foreign_key_check")],
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _online_backup(source: Path, backup_dir: Path, label: str) -> dict[str, Any]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    temporary = backup_dir / f".{label}-{uuid.uuid4().hex}.tmp"
    try:
        with closing(_readonly_connection(source)) as origin, closing(sqlite3.connect(temporary)) as destination:
            origin.backup(destination)
            check = destination.execute("PRAGMA integrity_check").fetchone()[0]
            if check != "ok":
                raise MergeError(f"Backup integrity check failed for {label}: {check}")
        digest = _sha256(temporary)
        backup = backup_dir / f"{label}-{digest[:16]}.sqlite3"
        if backup.exists():
            if _sha256(backup) != digest:
                raise MergeError(f"Existing backup has unexpected contents: {backup}")
            temporary.unlink()
        else:
            os.chmod(temporary, 0o600)
            temporary.replace(backup)
        return {"label": label, "path": str(backup), "sha256": digest}
    finally:
        temporary.unlink(missing_ok=True)


def _current_table_order() -> list[str]:
    return [table.name for table in Base.metadata.sorted_tables]


def _current_table_schema() -> dict[str, Any]:
    engine = create_engine("sqlite://")
    try:
        Base.metadata.create_all(engine)
        with engine.connect() as sql:
            connection = sql.connection.driver_connection
            connection.row_factory = sqlite3.Row
            schema = _schema_snapshot(connection)
            schema["alembic_version"] = {
                "columns": [
                    {"cid": 0, "name": "version_num", "type": "VARCHAR(32)", "notnull": 1, "dflt_value": None, "pk": 1}
                ],
                "indexes": [{"unique": True, "columns": ["version_num"], "origin": "pk", "partial": False}],
                "foreign_keys": [],
            }
            return schema
    finally:
        engine.dispose()


def _unique_constraints(connection: sqlite3.Connection, table: str) -> list[tuple[str, ...]]:
    constraints: list[tuple[str, ...]] = []
    pk_columns = tuple(
        row[1]
        for row in sorted(
            (row for row in connection.execute(f'PRAGMA table_info("{table}")') if row[5]),
            key=lambda row: row[5],
        )
    )
    if pk_columns:
        constraints.append(pk_columns)
    for index in connection.execute(f'PRAGMA index_list("{table}")'):
        if index[2]:
            columns = tuple(
                row[2]
                for row in connection.execute(f'PRAGMA index_info("{index[1]}")')
                if row[2] is not None
            )
            if columns and columns not in constraints:
                constraints.append(columns)
    return constraints


def _primary_key(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return next(iter(_unique_constraints(connection, table)), ())


def _find_row(
    connection: sqlite3.Connection, table: str, columns: tuple[str, ...], values: dict[str, Any]
) -> sqlite3.Row | None:
    if not columns or any(values.get(name) is None for name in columns):
        return None
    where = " AND ".join(f'"{name}" IS ?' for name in columns)
    return connection.execute(
        f'SELECT * FROM "{table}" WHERE {where} LIMIT 1', tuple(values[name] for name in columns)
    ).fetchone()


def _differs(existing: sqlite3.Row, values: dict[str, Any], primary_key: tuple[str, ...]) -> bool:
    return any(
        existing[name] != value
        for name, value in values.items()
        if name not in primary_key and name in existing.keys()
    )


def _foreign_keys(connection: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(f'PRAGMA foreign_key_list("{table}")')]


def _source_pk_maps(connection: sqlite3.Connection, order: list[str]) -> dict[str, dict[Any, Any]]:
    return {table: {} for table in order}


def _insert_source(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    source_label: str,
    order: list[str],
    report: dict[str, Any],
) -> None:
    maps = _source_pk_maps(source, order)
    target_tables = set(_table_names(target))
    for table in order:
        if table not in target_tables:
            continue
        source_tables = set(_table_names(source))
        if table not in source_tables:
            report["missing_source_tables"].setdefault(source_label, []).append(table)
            continue

        target_columns = [row[1] for row in target.execute(f'PRAGMA table_info("{table}")')]
        target_column_info = {row[1]: row for row in target.execute(f'PRAGMA table_info("{table}")')}
        source_columns = {row[1] for row in source.execute(f'PRAGMA table_info("{table}")')}
        missing_required = [
            name
            for name, info in target_column_info.items()
            if name not in source_columns and info[3] and info[4] is None
        ]
        if missing_required:
            raise MergeError(
                f"{source_label}.{table} lacks required current-schema columns: {', '.join(missing_required)}"
            )

        pk = _primary_key(target, table)
        natural_keys = list(NATURAL_KEYS.get(table, ()))
        for unique in _unique_constraints(target, table):
            if unique != pk and unique not in natural_keys:
                natural_keys.append(unique)
        surrogate_pk = len(pk) == 1 and pk[0] == "id" and str(target_column_info["id"][2]).upper() == "INTEGER"
        maximum_id = 0
        if surrogate_pk:
            maximum_id = int(target.execute(f'SELECT COALESCE(MAX("id"), 0) FROM "{table}"').fetchone()[0])

        insert_columns = [name for name in target_columns if name in source_columns]
        quoted_columns = ", ".join(f'"{name}"' for name in insert_columns)
        placeholders = ", ".join("?" for _ in insert_columns)
        ordering = ", ".join(f'"{name}"' for name in pk)
        cursor = source.execute(f'SELECT * FROM "{table}"' + (f" ORDER BY {ordering}" if ordering else ""))
        for source_row in cursor:
            values = {name: source_row[name] for name in source_columns if name in target_column_info}

            # Repoint foreign keys after an integer primary key was remapped.
            orphan = None
            for fk in _foreign_keys(source, table):
                child_column = fk["from"]
                parent_table = fk["table"]
                parent_column = fk["to"]
                original = values.get(child_column)
                parent_pk = _primary_key(target, parent_table) if parent_table in target_tables else ()
                if original is not None and parent_table in maps and len(parent_pk) == 1 and parent_column == parent_pk[0]:
                    values[child_column] = maps[parent_table].get(original, original)
                if values.get(child_column) is not None:
                    parent = _find_row(target, parent_table, (parent_column,), {parent_column: values[child_column]})
                    if parent is None:
                        orphan = {
                            "source": source_label,
                            "table": table,
                            "row_key": {name: source_row[name] for name in pk},
                            "foreign_key": f"{child_column}->{parent_table}.{parent_column}",
                            "value": original,
                        }
                        break
            if orphan:
                report["orphan_rows"].append(orphan)
                report["table_actions"][source_label][table].update({"skipped_orphan": 1})
                continue

            existing = None
            matched_key: tuple[str, ...] | None = None
            for key in natural_keys:
                if all(name in values for name in key):
                    existing = _find_row(target, table, key, values)
                    if existing is not None:
                        matched_key = key
                        break
            if existing is not None:
                if pk:
                    maps[table][source_row[pk[0]]] = existing[pk[0]]
                action = "deduplicated_equal" if not _differs(existing, values, pk) else "kept_primary_conflict"
                report["table_actions"][source_label][table].update({action: 1})
                if action == "kept_primary_conflict":
                    report["conflicts"].append(
                        {
                            "source": source_label,
                            "table": table,
                            "key_columns": list(matched_key or ()),
                            "key": {name: values.get(name) for name in (matched_key or ())},
                            "resolution": "kept_primary_row; source row remains in its verified source backup",
                        }
                    )
                continue

            if surrogate_pk and source_row[pk[0]] is not None:
                collision = _find_row(target, table, pk, values)
                if collision is not None:
                    maximum_id += 1
                    values[pk[0]] = maximum_id
                    report["table_actions"][source_label][table].update({"remapped_primary_key": 1})

            try:
                target.execute(
                    f'INSERT INTO "{table}" ({quoted_columns}) VALUES ({placeholders})',
                    tuple(values[name] for name in insert_columns),
                )
            except sqlite3.IntegrityError as error:
                raise MergeError(f"Cannot safely merge {source_label}.{table}: {error}") from error

            if pk:
                maps[table][source_row[pk[0]]] = values.get(pk[0], source_row[pk[0]])
            report["table_actions"][source_label][table].update({"inserted": 1})


def _dataset_summary(path: Path) -> dict[str, Any]:
    engine = create_engine(f"sqlite:///file:{path.resolve().as_posix()}?mode=ro&uri=true")
    try:
        with sessionmaker(bind=engine)() as session:
            dataset = RaceRepository(session).training_dataset()
        rows = dataset.get("rows", [])
        versions = Counter(str(row.get("feature_version") or "unknown") for row in rows)
        return {
            "training_rows": int(dataset["row_count"]),
            "training_races": int(dataset["race_count"]),
            "feature_count": len(dataset["feature_columns"]),
            "feature_columns": dataset["feature_columns"],
            "feature_versions": dict(sorted(versions.items())),
            "excluded_counts": dataset["excluded_counts"],
        }
    finally:
        engine.dispose()


def _review_exclusion_report(path: Path) -> dict[str, Any]:
    engine = create_engine(f"sqlite:///file:{path.resolve().as_posix()}?mode=ro&uri=true")
    try:
        with sessionmaker(bind=engine)() as session:
            repository = RaceRepository(session)
            ranked = select(
                PredictionReview.id.label("review_id"),
                func.row_number()
                .over(
                    partition_by=PredictionReview.race_id,
                    order_by=(PredictionReview.created_at.desc(), PredictionReview.id.desc()),
                )
                .label("review_rank"),
            ).subquery()
            reviews = session.scalars(
                select(PredictionReview)
                .join(ranked, ranked.c.review_id == PredictionReview.id)
                .where(ranked.c.review_rank == 1)
                .order_by(PredictionReview.race_id)
            ).all()
            race_ids = {row.race_id for row in reviews}
            races = {
                row.race_id: row
                for row in session.scalars(select(Race).where(Race.race_id.in_(race_ids)))
            }
            snapshots_by_race: dict[str, list[PredictionSnapshot]] = defaultdict(list)
            for snapshot in session.scalars(select(PredictionSnapshot).where(PredictionSnapshot.race_id.in_(race_ids))):
                snapshots_by_race[snapshot.race_id].append(snapshot)
            results = {
                row.race_id: row
                for row in session.scalars(select(RaceResult).where(RaceResult.race_id.in_(race_ids)))
            }
            result_entries_by_race: dict[str, set[int]] = defaultdict(set)
            for row in session.scalars(select(RaceResultEntry).where(RaceResultEntry.race_id.in_(race_ids))):
                result_entries_by_race[row.race_id].add(row.horse_number)

            exclusions: list[dict[str, Any]] = []
            reasons: Counter[str] = Counter()
            included = 0
            no_result = 0
            for review in reviews:
                race = races.get(review.race_id)
                rows = (review.payload or {}).get("rows", [])
                labeled = {
                    int(row["horse_number"])
                    for row in rows
                    if row.get("horse_number") not in (None, "") and row.get("actual_win") is not None
                }
                snapshots = snapshots_by_race.get(review.race_id, [])
                reason: str | None = None
                detail: str | None = None
                eligible_horse_numbers: set[int] = set()
                if race is None:
                    reason, detail = "missing_race", "race row is absent"
                elif not is_supported_flat_race(
                    repository._field_value(race.payload or {}, "surface"),
                    repository._field_value(race.payload or {}, "race_class"),
                ):
                    reason, detail = "unsupported_race_type", "race is not a supported flat race"
                elif not rows or not labeled:
                    reason, detail = "missing_label", "review has no horse row with horse_number and actual_win"
                elif not snapshots:
                    reason, detail = "missing_snapshot", "race has no prediction snapshot"
                else:
                    start = race_start_datetime(
                        race.race_date, repository._field_value(race.payload or {}, "start_time")
                    )
                    if start is None:
                        reason, detail = "invalid_prediction_time", "race date or start time is unavailable"
                    else:
                        result = results.get(review.race_id)
                        cutoff = start.astimezone(timezone.utc).replace(tzinfo=None)
                        for snapshot in snapshots:
                            timestamp = snapshot.prediction_timestamp
                            if timestamp is None or timestamp >= cutoff:
                                continue
                            if result is not None and result.stored_at is not None:
                                result_stored_at = result.stored_at
                                if result_stored_at.tzinfo is not None:
                                    result_stored_at = result_stored_at.astimezone(timezone.utc).replace(tzinfo=None)
                                if timestamp > result_stored_at:
                                    continue
                            eligible_horse_numbers.add(snapshot.horse_number)
                        if not eligible_horse_numbers:
                            reason, detail = "invalid_prediction_time", "all snapshots are at or after race start or result storage"
                        elif not (eligible_horse_numbers & labeled):
                            reason, detail = "horse_number_mismatch", "eligible snapshots do not match labeled review rows"

                if review.race_id not in results:
                    no_result += 1
                if reason is None:
                    included += 1
                else:
                    reasons[reason] += 1
                    exclusions.append(
                        {
                            "race_id": review.race_id,
                            "reason": reason,
                            "detail": detail,
                            "review_rows": len(rows),
                            "labeled_horses": len(labeled),
                            "snapshots": len(snapshots),
                            "result_entries": len(result_entries_by_race.get(review.race_id, set())),
                        }
                    )
            return {
                "reviewed_races": len(reviews),
                "training_eligible_races": included,
                "excluded_races": sum(reasons.values()),
                "excluded_by_reason": dict(sorted(reasons.items())),
                "reviewed_races_without_result_row": no_result,
                "excluded": exclusions,
            }
    finally:
        engine.dispose()


def _schema_diff(primary: dict[str, Any], secondary: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    all_tables = sorted(set(primary) | set(secondary) | set(current))
    differences = {}
    for table in all_tables:
        left = primary.get(table, {})
        right = secondary.get(table, {})
        now = current.get(table, {})
        sets = {
            "primary_columns": {column["name"] for column in left.get("columns", [])},
            "secondary_columns": {column["name"] for column in right.get("columns", [])},
            "current_columns": {column["name"] for column in now.get("columns", [])},
            "primary_column_definitions": {
                column["name"]: (column["type"], bool(column["notnull"]), column["dflt_value"], column["pk"])
                for column in left.get("columns", [])
            },
            "secondary_column_definitions": {
                column["name"]: (column["type"], bool(column["notnull"]), column["dflt_value"], column["pk"])
                for column in right.get("columns", [])
            },
            "current_column_definitions": {
                column["name"]: (column["type"], bool(column["notnull"]), column["dflt_value"], column["pk"])
                for column in now.get("columns", [])
            },
            "primary_indexes": {
                (index["unique"], tuple(index["columns"]), index.get("origin"), index.get("partial", False))
                for index in left.get("indexes", [])
            },
            "secondary_indexes": {
                (index["unique"], tuple(index["columns"]), index.get("origin"), index.get("partial", False))
                for index in right.get("indexes", [])
            },
            "current_indexes": {
                (index["unique"], tuple(index["columns"]), index.get("origin"), index.get("partial", False))
                for index in now.get("indexes", [])
            },
            "primary_foreign_keys": {
                (
                    fk["table"], fk["from"], fk["to"], fk.get("sequence", 0),
                    fk.get("on_update"), fk.get("on_delete"), fk.get("match"),
                )
                for fk in left.get("foreign_keys", [])
            },
            "secondary_foreign_keys": {
                (
                    fk["table"], fk["from"], fk["to"], fk.get("sequence", 0),
                    fk.get("on_update"), fk.get("on_delete"), fk.get("match"),
                )
                for fk in right.get("foreign_keys", [])
            },
            "current_foreign_keys": {
                (
                    fk["table"], fk["from"], fk["to"], fk.get("sequence", 0),
                    fk.get("on_update"), fk.get("on_delete"), fk.get("match"),
                )
                for fk in now.get("foreign_keys", [])
            },
        }
        values = {
            "primary_only_columns": sorted(sets["primary_columns"] - sets["secondary_columns"]),
            "secondary_only_columns": sorted(sets["secondary_columns"] - sets["primary_columns"]),
            "current_only_columns": sorted(sets["current_columns"] - (sets["primary_columns"] | sets["secondary_columns"])),
            "primary_vs_secondary_column_definition_differences": {
                name: {
                    "primary": sets["primary_column_definitions"].get(name),
                    "secondary": sets["secondary_column_definitions"].get(name),
                }
                for name in sorted(sets["primary_columns"] & sets["secondary_columns"])
                if sets["primary_column_definitions"].get(name) != sets["secondary_column_definitions"].get(name)
            },
            "primary_vs_current_column_definition_differences": {
                name: {
                    "primary": sets["primary_column_definitions"].get(name),
                    "current": sets["current_column_definitions"].get(name),
                }
                for name in sorted(sets["primary_columns"] & sets["current_columns"])
                if sets["primary_column_definitions"].get(name) != sets["current_column_definitions"].get(name)
            },
            "secondary_vs_current_column_definition_differences": {
                name: {
                    "secondary": sets["secondary_column_definitions"].get(name),
                    "current": sets["current_column_definitions"].get(name),
                }
                for name in sorted(sets["secondary_columns"] & sets["current_columns"])
                if sets["secondary_column_definitions"].get(name) != sets["current_column_definitions"].get(name)
            },
            "primary_only_indexes": sorted(map(list, sets["primary_indexes"] - sets["secondary_indexes"])),
            "secondary_only_indexes": sorted(map(list, sets["secondary_indexes"] - sets["primary_indexes"])),
            "current_only_indexes": sorted(
                map(list, sets["current_indexes"] - (sets["primary_indexes"] | sets["secondary_indexes"]))
            ),
            "primary_only_foreign_keys": sorted(map(list, sets["primary_foreign_keys"] - sets["secondary_foreign_keys"])),
            "secondary_only_foreign_keys": sorted(map(list, sets["secondary_foreign_keys"] - sets["primary_foreign_keys"])),
            "current_only_foreign_keys": sorted(
                map(list, sets["current_foreign_keys"] - (sets["primary_foreign_keys"] | sets["secondary_foreign_keys"]))
            ),
            "source_only_table": table not in current,
            "missing_from_primary": table not in primary,
            "missing_from_secondary": table not in secondary,
        }
        if any(values.values()):
            differences[table] = values
    return differences


def _connection_report(path: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, int]]:
    with closing(_readonly_connection(path)) as connection:
        schema = _schema_snapshot(connection)
        integrity = _integrity(connection)
        counts = _counts(connection)
        return schema, integrity, counts


def _version_report(path: Path) -> dict[str, Any]:
    with closing(_readonly_connection(path)) as connection:
        versions = []
        if "alembic_version" in _table_names(connection):
            versions = [
                row[0]
                for row in connection.execute("SELECT version_num FROM alembic_version ORDER BY version_num")
            ]
        return {
            "alembic_versions": versions,
            "sqlite_user_version": int(connection.execute("PRAGMA user_version").fetchone()[0]),
        }


def _semantic_duplicate_report(connection: sqlite3.Connection) -> dict[str, Any]:
    report: dict[str, Any] = {}
    available = set(_table_names(connection))
    for table, keys in NATURAL_KEYS.items():
        if table not in available:
            continue
        columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
        for key in keys:
            if not set(key).issubset(columns):
                continue
            group_columns = ", ".join(f'"{column}"' for column in key)
            non_null = " AND ".join(f'"{column}" IS NOT NULL' for column in key)
            groups = connection.execute(
                f'SELECT {group_columns}, COUNT(*) AS row_count FROM "{table}" '
                f"WHERE {non_null} GROUP BY {group_columns} HAVING COUNT(*) > 1"
            ).fetchall()
            if groups:
                report[f"{table}:{','.join(key)}"] = {
                    "duplicate_groups": len(groups),
                    "examples": [dict(row) for row in groups[:10]],
                }
    return report


def merge_databases(
    primary_path: Path,
    secondary_path: Path,
    output_path: Path,
    backup_dir: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    primary_path = primary_path.expanduser().resolve()
    secondary_path = secondary_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    if primary_path == secondary_path:
        raise MergeError("Primary and secondary database paths must be different")
    if output_path in (primary_path, secondary_path):
        raise MergeError("Output must be a new path and cannot overwrite either source database")
    if output_path.exists():
        raise FileExistsError(f"Refusing to overwrite existing output database: {output_path}")

    backup_records = []
    primary_snapshot = primary_path
    secondary_snapshot = secondary_path
    if not dry_run:
        if backup_dir is None:
            backup_dir = output_path.parent / "backups" / "issue38"
        backup_records = [
            _online_backup(primary_path, backup_dir, "primary"),
            _online_backup(secondary_path, backup_dir, "secondary"),
        ]
        primary_snapshot = Path(backup_records[0]["path"])
        secondary_snapshot = Path(backup_records[1]["path"])

    primary_schema, primary_integrity, primary_counts = _connection_report(primary_snapshot)
    secondary_schema, secondary_integrity, secondary_counts = _connection_report(secondary_snapshot)
    if primary_integrity["integrity_check"] != ["ok"]:
        raise MergeError(f"Primary database failed integrity_check: {primary_integrity['integrity_check']}")
    if secondary_integrity["integrity_check"] != ["ok"]:
        raise MergeError(f"Secondary database failed integrity_check: {secondary_integrity['integrity_check']}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    staging = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp")
    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "primary": {
            "path": str(primary_path),
            "snapshot_path": str(primary_snapshot),
            "tables": primary_counts,
            "integrity": primary_integrity,
        },
        "secondary": {
            "path": str(secondary_path),
            "snapshot_path": str(secondary_snapshot),
            "tables": secondary_counts,
            "integrity": secondary_integrity,
        },
        "schema_versions": {
            "primary": _version_report(primary_snapshot),
            "secondary": _version_report(secondary_snapshot),
            "merged": {"alembic_versions": [CURRENT_ALEMBIC_REVISION], "sqlite_user_version": 0},
        },
        "backups": backup_records,
        "schema_diff": _schema_diff(primary_schema, secondary_schema, _current_table_schema()),
        "table_actions": {
            "primary": defaultdict(lambda: defaultdict(Counter)),
            "secondary": defaultdict(lambda: defaultdict(Counter)),
        },
        "missing_source_tables": {},
        "orphan_rows": [],
        "conflicts": [],
        "safe_feature_backfill": {
            "status": "not_performed",
            "features": [],
            "reason": "Historical snapshots are immutable. Stored past-performance rows do not prove that the complete feature inputs were available by each prediction timestamp; unknown inputs remain missing.",
        },
    }
    try:
        engine = create_engine(f"sqlite:///{staging}")
        try:
            Base.metadata.create_all(engine)
            with engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                target = connection.connection.driver_connection
                target.row_factory = sqlite3.Row
                target.execute("BEGIN IMMEDIATE")
                target.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
                target.execute(
                    "INSERT INTO alembic_version (version_num) VALUES (?)",
                    (CURRENT_ALEMBIC_REVISION,),
                )
                order = _current_table_order()
                with closing(_readonly_connection(primary_snapshot)) as primary:
                    _insert_source(primary, target, "primary", order, report)
                with closing(_readonly_connection(secondary_snapshot)) as secondary:
                    _insert_source(secondary, target, "secondary", order, report)
                violations = [list(row) for row in target.execute("PRAGMA foreign_key_check")]
                if violations:
                    raise MergeError(f"Merged candidate has foreign-key violations: {violations[:10]}")
                report["duplicate_natural_keys"] = _semantic_duplicate_report(target)
                if report["duplicate_natural_keys"]:
                    raise MergeError(f"Merged candidate has duplicate natural keys: {report['duplicate_natural_keys']}")
                target.execute("COMMIT")
                target.execute("PRAGMA integrity_check")
                report["merged_tables"] = _counts(target, order)
                report["merged_integrity"] = _integrity(target)
                target.commit()
        finally:
            engine.dispose()

        report["table_actions"] = {
            source: {table: dict(counts) for table, counts in tables.items()}
            for source, tables in report["table_actions"].items()
        }
        action_totals = Counter()
        for table_actions in report["table_actions"].values():
            for actions in table_actions.values():
                action_totals.update(actions)
        report["duplicate_summary"] = {
            "identical_natural_key_rows_skipped": action_totals["deduplicated_equal"],
            "conflicting_natural_key_rows_kept_from_primary": action_totals["kept_primary_conflict"],
            "surrogate_primary_keys_remapped": action_totals["remapped_primary_key"],
            "orphan_rows_skipped": len(report["orphan_rows"]),
            "post_merge_duplicate_natural_keys": sum(
                item["duplicate_groups"] for item in report["duplicate_natural_keys"].values()
            ),
        }
        report["source_review_diagnostics"] = {
            "primary": _review_exclusion_report(primary_snapshot),
            "secondary": _review_exclusion_report(secondary_snapshot),
        }
        report["merged_review_diagnostics"] = _review_exclusion_report(staging)
        report["training_dataset"] = {
            "primary": _dataset_summary(primary_snapshot),
            "secondary": _dataset_summary(secondary_snapshot),
            "merged": _dataset_summary(staging),
        }
        primary_features = set(report["training_dataset"]["primary"]["feature_columns"])
        secondary_features = set(report["training_dataset"]["secondary"]["feature_columns"])
        report["feature_diff"] = {
            "primary_only": sorted(primary_features - secondary_features),
            "secondary_only": sorted(secondary_features - primary_features),
            "merged_feature_versions": report["training_dataset"]["merged"]["feature_versions"],
        }

        if not dry_run:
            os.chmod(staging, 0o600)
            staging.replace(output_path)
            report["output_path"] = str(output_path)
        else:
            report["output_path"] = None
        return report
    finally:
        staging.unlink(missing_ok=True)


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--primary", required=True, type=Path, help="Current-schema database whose duplicate rows win")
    parser.add_argument("--secondary", required=True, type=Path, help="Database to merge into the new candidate")
    parser.add_argument("--output", required=True, type=Path, help="New candidate database path; existing files are never overwritten")
    parser.add_argument("--backup-dir", type=Path, help="Directory for verified online backups")
    parser.add_argument("--report", type=Path, help="Write the JSON merge report to this path")
    parser.add_argument("--dry-run", action="store_true", help="Build and inspect a temporary candidate without backups or final output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _argument_parser().parse_args(argv)
    try:
        report = merge_databases(
            args.primary,
            args.secondary,
            args.output,
            backup_dir=args.backup_dir,
            dry_run=args.dry_run,
        )
    except (FileNotFoundError, FileExistsError, MergeError, sqlite3.Error, OSError) as error:
        print(f"Database merge failed: {error}", file=sys.stderr)
        return 1
    rendered = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.report.with_name(f".{args.report.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(rendered + "\n", encoding="utf-8")
        temporary.replace(args.report)
        print(f"Merge report: {args.report}")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
