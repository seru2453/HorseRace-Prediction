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


def _backfill_normalized_tables(factory, models) -> None:
    from services.ml.targets import relative_finish, relative_finish_category
    from services.common.race_eligibility import prediction_lead_minutes

    with factory() as session:
        snapshots = session.scalars(
            select(models.PredictionSnapshot).where(
                ~models.PredictionSnapshot.id.in_(select(models.PredictionMetric.prediction_snapshot_id))
            )
        ).all()
        for snapshot in snapshots:
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

        for metric, snapshot in session.execute(
            select(models.PredictionMetric, models.PredictionSnapshot).join(
                models.PredictionSnapshot,
                models.PredictionSnapshot.id == models.PredictionMetric.prediction_snapshot_id,
            )
        ).all():
            payload = snapshot.prediction_payload or {}
            metric.current_form = metric.current_form if metric.current_form is not None else payload.get("current_form")
            metric.condition_fit = metric.condition_fit if metric.condition_fit is not None else payload.get("condition_fit")
            metric.previous_race_residual = (
                metric.previous_race_residual
                if metric.previous_race_residual is not None
                else payload.get("previous_race_residual")
            )
            metric.rebound_score = metric.rebound_score if metric.rebound_score is not None else payload.get("rebound_score")
            metric.field_size = metric.field_size if metric.field_size is not None else payload.get("field_size")
            metric.jev_features = metric.jev_features or payload.get("jev_features") or None

        results = session.scalars(select(models.RaceResult)).all()
        for result in results:
            race = session.get(models.Race, result.race_id)
            race_payload = race.payload if race else {}
            raw_field_size = (race_payload or {}).get("field_size")
            if isinstance(raw_field_size, dict):
                raw_field_size = raw_field_size.get("value")
            result_field_size = int(raw_field_size or len((result.payload or {}).get("entries", []))) or None
            existing_numbers = set(
                session.scalars(
                    select(models.RaceResultEntry.horse_number).where(
                        models.RaceResultEntry.race_id == result.race_id
                    )
                ).all()
            )
            race_entries = {
                row.horse_number: row
                for row in session.scalars(
                    select(models.RaceEntry).where(models.RaceEntry.race_id == result.race_id)
                ).all()
            }
            for item in (result.payload or {}).get("entries", []):
                horse_number = item.get("horse_number")
                if horse_number is None or int(horse_number) in existing_numbers:
                    continue
                horse_number = int(horse_number)
                race_entry = race_entries.get(horse_number)
                session.add(
                    models.RaceResultEntry(
                        race_id=result.race_id,
                        horse_id=race_entry.horse_id if race_entry else None,
                        horse_number=horse_number,
                        finish_position=item.get("finish_position"),
                        result_status=item.get("result_status"),
                        finish_time=item.get("finish_time"),
                        margin=item.get("margin"),
                        final_three_furlongs=item.get("final_three_furlongs"),
                        corner_positions=item.get("corner_positions"),
                        horse_weight=item.get("horse_weight"),
                        horse_weight_change=item.get("horse_weight_change"),
                        jockey_name=item.get("jockey_name"),
                        popularity=item.get("popularity"),
                        field_size=result_field_size,
                        relative_finish=relative_finish(item.get("finish_position"), result_field_size),
                        relative_finish_category=relative_finish_category(
                            relative_finish(item.get("finish_position"), result_field_size)
                        ),
                    )
                )
            for row in session.scalars(
                select(models.RaceResultEntry).where(models.RaceResultEntry.race_id == result.race_id)
            ).all():
                if row.field_size is None:
                    row.field_size = result_field_size
                if row.relative_finish is None:
                    row.relative_finish = relative_finish(row.finish_position, row.field_size)
                    row.relative_finish_category = relative_finish_category(row.relative_finish)

        races = {row.race_id: row for row in session.scalars(select(models.Race)).all()}
        pre_race_snapshots = {}
        for snapshot in session.scalars(
            select(models.PredictionSnapshot).order_by(
                models.PredictionSnapshot.prediction_timestamp.desc(),
                models.PredictionSnapshot.id.desc(),
            )
        ).all():
            race = races.get(snapshot.race_id)
            if race is None:
                continue
            start_time = (race.payload or {}).get("start_time")
            if isinstance(start_time, dict):
                start_time = start_time.get("value")
            lead = prediction_lead_minutes(race.race_date, start_time, snapshot.prediction_timestamp)
            if lead is not None and lead > 0:
                pre_race_snapshots.setdefault((snapshot.race_id, snapshot.horse_id), snapshot)

        result_entries = {
            (row.race_id, row.horse_id): row
            for row in session.scalars(select(models.RaceResultEntry)).all()
            if row.horse_id is not None
        }
        for performance in session.scalars(
            select(models.PastPerformance).where(models.PastPerformance.observed_in_race_id.is_not(None))
        ).all():
            race_id = performance.observed_in_race_id
            result_entry = result_entries.get((race_id, performance.horse_id))
            if result_entry is None:
                continue
            payload = dict(performance.payload or {})
            actual = relative_finish(result_entry.finish_position, result_entry.field_size)
            snapshot = pre_race_snapshots.get((race_id, performance.horse_id))
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
