from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


def make_engine(database_url: str | None = None):
    if database_url is None:
        db_path = Path("data/db/keiba.sqlite3")
        db_path.parent.mkdir(parents=True, exist_ok=True)
        database_url = f"sqlite:///{db_path}"
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, connect_args=connect_args)


def make_session_factory(database_url: str | None = None):
    from database import models as database_models

    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    _backfill_normalized_tables(factory, database_models)
    return factory


@lru_cache(maxsize=16)
def get_session_factory(database_url: str | None = None):
    """Return a process-wide factory so schema checks and legacy backfills run once per database."""
    return make_session_factory(database_url)


BACKFILL_CHUNK_SIZE = 500


def _backfill_normalized_tables(factory, models) -> None:
    from services.ml.targets import relative_finish, relative_finish_category
    from database.repository import RaceRepository

    with factory() as session:
        last_snapshot_id = 0
        while True:
            snapshot_ids = session.scalars(
                select(models.PredictionSnapshot.id)
                .where(
                    models.PredictionSnapshot.id > last_snapshot_id,
                    ~models.PredictionSnapshot.id.in_(select(models.PredictionMetric.prediction_snapshot_id)),
                )
                .order_by(models.PredictionSnapshot.id)
                .limit(BACKFILL_CHUNK_SIZE)
            ).all()
            if not snapshot_ids:
                break
            for snapshot in session.scalars(
                select(models.PredictionSnapshot).where(models.PredictionSnapshot.id.in_(snapshot_ids))
            ):
                payload = snapshot.prediction_payload or {}
                session.add(
                    models.PredictionMetric(
                        prediction_snapshot_id=snapshot.id,
                        base_score=payload.get("base_score"),
                        condition_adjustment=payload.get("condition_adjustment"),
                        uncertainty=payload.get("uncertainty"),
                        confidence=payload.get("confidence"),
                        base_rank=payload.get("base_rank"),
                        final_rank=payload.get("final_rank"),
                        rank_delta=payload.get("rank_delta"),
                        current_form=payload.get("current_form"),
                        condition_fit=payload.get("condition_fit"),
                        previous_race_residual=payload.get("previous_race_residual"),
                        rebound_score=payload.get("rebound_score"),
                        field_size=payload.get("field_size"),
                        jev_features=payload.get("jev_features") or None,
                    )
                )
            session.commit()
            last_snapshot_id = snapshot_ids[-1]

        last_metric_id = 0
        while True:
            metric_ids = session.scalars(
                select(models.PredictionMetric.prediction_snapshot_id)
                .where(models.PredictionMetric.prediction_snapshot_id > last_metric_id)
                .order_by(models.PredictionMetric.prediction_snapshot_id)
                .limit(BACKFILL_CHUNK_SIZE)
            ).all()
            if not metric_ids:
                break
            pairs = session.execute(
                select(models.PredictionMetric, models.PredictionSnapshot)
                .join(
                    models.PredictionSnapshot,
                    models.PredictionSnapshot.id == models.PredictionMetric.prediction_snapshot_id,
                )
                .where(models.PredictionMetric.prediction_snapshot_id.in_(metric_ids))
            ).all()
            for metric, snapshot in pairs:
                payload = snapshot.prediction_payload or {}
                metric.current_form = metric.current_form if metric.current_form is not None else payload.get("current_form")
                metric.condition_fit = metric.condition_fit if metric.condition_fit is not None else payload.get("condition_fit")
                metric.previous_race_residual = (
                    metric.previous_race_residual if metric.previous_race_residual is not None
                    else payload.get("previous_race_residual")
                )
                metric.rebound_score = metric.rebound_score if metric.rebound_score is not None else payload.get("rebound_score")
                metric.field_size = metric.field_size if metric.field_size is not None else payload.get("field_size")
                metric.jev_features = metric.jev_features or payload.get("jev_features") or None
            session.commit()
            last_metric_id = metric_ids[-1]

        last_result_race_id = ""
        while True:
            results = session.scalars(
                select(models.RaceResult)
                .where(models.RaceResult.race_id > last_result_race_id)
                .order_by(models.RaceResult.race_id)
                .limit(BACKFILL_CHUNK_SIZE)
            ).all()
            if not results:
                break
            race_ids = [result.race_id for result in results]
            races = {
                row.race_id: row
                for row in session.scalars(select(models.Race).where(models.Race.race_id.in_(race_ids)))
            }
            race_entries = session.scalars(select(models.RaceEntry).where(models.RaceEntry.race_id.in_(race_ids))).all()
            entries_by_race_number = {
                (row.race_id, row.horse_number): row for row in race_entries
            }
            result_entries = session.scalars(
                select(models.RaceResultEntry).where(models.RaceResultEntry.race_id.in_(race_ids))
            ).all()
            result_entries_by_key = {(row.race_id, row.horse_number): row for row in result_entries}
            for result in results:
                race = races.get(result.race_id)
                race_payload = race.payload if race else {}
                raw_field_size = (race_payload or {}).get("field_size")
                if isinstance(raw_field_size, dict):
                    raw_field_size = raw_field_size.get("value")
                result_items = (result.payload or {}).get("entries", [])
                field_size = int(raw_field_size or len(result_items)) or None
                for item in result_items:
                    if item.get("horse_number") is None:
                        continue
                    horse_number = int(item["horse_number"])
                    key = (result.race_id, horse_number)
                    row = result_entries_by_key.get(key)
                    if row is None:
                        race_entry = entries_by_race_number.get(key)
                        position = item.get("finish_position")
                        relative_value = relative_finish(position, field_size)
                        row = models.RaceResultEntry(
                            race_id=result.race_id,
                            horse_id=race_entry.horse_id if race_entry else None,
                            horse_number=horse_number,
                            finish_position=position,
                            result_status=item.get("result_status"),
                            finish_time=item.get("finish_time"),
                            margin=item.get("margin"),
                            final_three_furlongs=item.get("final_three_furlongs"),
                            corner_positions=item.get("corner_positions"),
                            horse_weight=item.get("horse_weight"),
                            horse_weight_change=item.get("horse_weight_change"),
                            jockey_name=item.get("jockey_name"),
                            popularity=item.get("popularity"),
                            field_size=field_size,
                            relative_finish=relative_value,
                            relative_finish_category=relative_finish_category(relative_value),
                        )
                        session.add(row)
                        result_entries_by_key[key] = row
                    if row.field_size is None:
                        row.field_size = field_size
                    if row.relative_finish is None:
                        row.relative_finish = relative_finish(row.finish_position, row.field_size)
                        row.relative_finish_category = relative_finish_category(row.relative_finish)
            session.commit()
            last_result_race_id = results[-1].race_id

        last_performance_id = 0
        while True:
            performance_ids = session.scalars(
                select(models.PastPerformance.id)
                .where(
                    models.PastPerformance.id > last_performance_id,
                    models.PastPerformance.observed_in_race_id.is_not(None),
                )
                .order_by(models.PastPerformance.id)
                .limit(BACKFILL_CHUNK_SIZE)
            ).all()
            if not performance_ids:
                break
            performances = session.scalars(
                select(models.PastPerformance).where(models.PastPerformance.id.in_(performance_ids))
            ).all()
            race_ids = {row.observed_in_race_id for row in performances if row.observed_in_race_id}
            horse_ids = {row.horse_id for row in performances}
            result_entries = session.scalars(
                select(models.RaceResultEntry).where(
                    models.RaceResultEntry.race_id.in_(race_ids),
                    models.RaceResultEntry.horse_id.in_(horse_ids),
                )
            ).all() if race_ids and horse_ids else []
            result_entries_by_key = {
                (row.race_id, row.horse_id): row for row in result_entries if row.horse_id is not None
            }
            snapshots = RaceRepository(session)._latest_snapshots_by_race_and_horse(race_ids)
            for performance in performances:
                race_id = performance.observed_in_race_id
                result_entry = result_entries_by_key.get((race_id, performance.horse_id))
                if result_entry is None:
                    continue
                payload = dict(performance.payload or {})
                actual = relative_finish(result_entry.finish_position, result_entry.field_size)
                snapshot = snapshots.get((race_id, result_entry.horse_number))
                final_rank = (snapshot.prediction_payload or {}).get("final_rank") if snapshot else None
                expected = relative_finish(final_rank, result_entry.field_size)
                payload.setdefault("actual_performance", actual)
                payload.setdefault("expected_performance", expected)
                payload.setdefault(
                    "performance_residual",
                    actual - expected if actual is not None and expected is not None else None,
                )
                performance.payload = payload
            session.commit()
            last_performance_id = performance_ids[-1]
