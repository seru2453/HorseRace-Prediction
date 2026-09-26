from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.session import Base


class Race(Base):
    __tablename__ = "races"

    race_id: Mapped[str] = mapped_column(String, primary_key=True)
    race_date: Mapped[str] = mapped_column(String, index=True)
    racecourse: Mapped[str] = mapped_column(String, index=True)
    race_number: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class Horse(Base):
    __tablename__ = "horses"

    horse_id: Mapped[str] = mapped_column(String, primary_key=True)
    horse_name: Mapped[str] = mapped_column(String, index=True)
    payload: Mapped[dict] = mapped_column(JSON)


class RaceEntry(Base):
    __tablename__ = "race_entries"
    __table_args__ = (UniqueConstraint("race_id", "horse_number", name="uq_race_horse_number"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    race_id: Mapped[str] = mapped_column(ForeignKey("races.race_id"), index=True)
    horse_id: Mapped[str] = mapped_column(ForeignKey("horses.horse_id"), index=True)
    horse_number: Mapped[int] = mapped_column(Integer)
    frame_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scratched: Mapped[bool] = mapped_column(Boolean, default=False)
    payload: Mapped[dict] = mapped_column(JSON)
    race: Mapped[Race] = relationship()
    horse: Mapped[Horse] = relationship()


class PastPerformance(Base):
    __tablename__ = "past_performances"
    __table_args__ = (UniqueConstraint("horse_id", "race_date", "racecourse", "race_name", name="uq_horse_past_race"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    horse_id: Mapped[str] = mapped_column(ForeignKey("horses.horse_id"), index=True)
    observed_in_race_id: Mapped[str | None] = mapped_column(ForeignKey("races.race_id"), nullable=True, index=True)
    race_date: Mapped[str] = mapped_column(String, index=True)
    racecourse: Mapped[str | None] = mapped_column(String, nullable=True)
    race_name: Mapped[str | None] = mapped_column(String, nullable=True)
    finish_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON)
    stored_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    horse: Mapped[Horse] = relationship()


class Prediction(Base):
    __tablename__ = "predictions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    race_id: Mapped[str] = mapped_column(ForeignKey("races.race_id"), index=True)
    horse_id: Mapped[str] = mapped_column(ForeignKey("horses.horse_id"), index=True)
    model_version: Mapped[str] = mapped_column(String)
    feature_version: Mapped[str] = mapped_column(String)
    win_probability: Mapped[float] = mapped_column(Float)
    payload: Mapped[dict] = mapped_column(JSON)
    prediction_timestamp: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class PredictionSnapshot(Base):
    __tablename__ = "prediction_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    race_id: Mapped[str] = mapped_column(ForeignKey("races.race_id"), index=True)
    horse_id: Mapped[str] = mapped_column(ForeignKey("horses.horse_id"), index=True)
    horse_number: Mapped[int] = mapped_column(Integer)
    prediction_timestamp: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    model_version: Mapped[str] = mapped_column(String)
    feature_version: Mapped[str] = mapped_column(String)
    simulation_seed: Mapped[int] = mapped_column(Integer)
    ai_win_probability: Mapped[float] = mapped_column(Float)
    ai_top2_probability: Mapped[float] = mapped_column(Float)
    ai_top3_probability: Mapped[float] = mapped_column(Float)
    raw_features: Mapped[dict] = mapped_column(JSON)
    prediction_payload: Mapped[dict] = mapped_column(JSON)


class PredictionMetric(Base):
    __tablename__ = "prediction_metrics"

    prediction_snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("prediction_snapshots.id"), primary_key=True
    )
    base_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    condition_adjustment: Mapped[float | None] = mapped_column(Float, nullable=True)
    uncertainty: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    base_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    final_rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rank_delta: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_form: Mapped[float | None] = mapped_column(Float, nullable=True)
    condition_fit: Mapped[float | None] = mapped_column(Float, nullable=True)
    previous_race_residual: Mapped[float | None] = mapped_column(Float, nullable=True)
    rebound_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    field_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    jev_features: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class ChallengerPrediction(Base):
    __tablename__ = "challenger_predictions"
    __table_args__ = (
        UniqueConstraint(
            "prediction_snapshot_id",
            "model_version",
            "target",
            name="uq_challenger_snapshot_model_target",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    prediction_snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("prediction_snapshots.id"), index=True
    )
    model_version: Mapped[str] = mapped_column(String, index=True)
    feature_version: Mapped[str | None] = mapped_column(String, nullable=True)
    target: Mapped[str] = mapped_column(String, index=True)
    predicted_value: Mapped[float] = mapped_column(Float)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


class AnalysisRun(Base):
    __tablename__ = "analysis_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    race_id: Mapped[str] = mapped_column(ForeignKey("races.race_id"), index=True)
    provider_name: Mapped[str] = mapped_column(String)
    parse_status: Mapped[str] = mapped_column(String, index=True)
    recognized_entries: Mapped[int] = mapped_column(Integer)
    expected_entries: Mapped[int | None] = mapped_column(Integer, nullable=True)
    issue_count: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class Simulation(Base):
    __tablename__ = "simulations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    race_id: Mapped[str] = mapped_column(ForeignKey("races.race_id"), index=True)
    iterations: Mapped[int] = mapped_column(Integer)
    seed: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class UserPrediction(Base):
    __tablename__ = "user_predictions"
    __table_args__ = (UniqueConstraint("race_id", "horse_number", "prediction_timestamp", name="uq_user_prediction_snapshot"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    race_id: Mapped[str] = mapped_column(ForeignKey("races.race_id"), index=True)
    horse_number: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSON)
    prediction_timestamp: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class RaceResult(Base):
    __tablename__ = "race_results"

    race_id: Mapped[str] = mapped_column(ForeignKey("races.race_id"), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)
    result_timestamp: Mapped[str] = mapped_column(String)
    stored_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class RaceResultEntry(Base):
    __tablename__ = "race_result_entries"
    __table_args__ = (UniqueConstraint("race_id", "horse_number", name="uq_result_race_horse_number"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    race_id: Mapped[str] = mapped_column(ForeignKey("race_results.race_id"), index=True)
    horse_id: Mapped[str | None] = mapped_column(ForeignKey("horses.horse_id"), nullable=True, index=True)
    horse_number: Mapped[int] = mapped_column(Integer)
    finish_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_status: Mapped[str | None] = mapped_column(String, nullable=True)
    finish_time: Mapped[str | None] = mapped_column(String, nullable=True)
    margin: Mapped[str | None] = mapped_column(String, nullable=True)
    final_three_furlongs: Mapped[float | None] = mapped_column(Float, nullable=True)
    corner_positions: Mapped[str | None] = mapped_column(String, nullable=True)
    horse_weight: Mapped[int | None] = mapped_column(Integer, nullable=True)
    horse_weight_change: Mapped[int | None] = mapped_column(Integer, nullable=True)
    jockey_name: Mapped[str | None] = mapped_column(String, nullable=True)
    popularity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    field_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    relative_finish: Mapped[float | None] = mapped_column(Float, nullable=True)
    relative_finish_category: Mapped[str | None] = mapped_column(String, nullable=True)


class PredictionReview(Base):
    __tablename__ = "prediction_reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    race_id: Mapped[str] = mapped_column(ForeignKey("races.race_id"), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class DataFetchLog(Base):
    __tablename__ = "data_fetch_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class ModelEvaluation(Base):
    __tablename__ = "model_evaluations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_version: Mapped[str] = mapped_column(String, index=True)
    feature_version: Mapped[str | None] = mapped_column(String, nullable=True)
    evaluation_period: Mapped[str] = mapped_column(String)
    brier_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    log_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    calibration_error: Mapped[float | None] = mapped_column(Float, nullable=True)
    top3_hit_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    race_count: Mapped[int] = mapped_column(Integer, default=0)
    horse_count: Mapped[int] = mapped_column(Integer, default=0)
    training_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("training_runs.id"), nullable=True, index=True
    )
    training_start: Mapped[str | None] = mapped_column(String, nullable=True)
    training_end: Mapped[str | None] = mapped_column(String, nullable=True)
    evaluation_start: Mapped[str | None] = mapped_column(String, nullable=True)
    evaluation_end: Mapped[str | None] = mapped_column(String, nullable=True)
    top3_recall: Mapped[float | None] = mapped_column(Float, nullable=True)
    winner_rank_mean: Mapped[float | None] = mapped_column(Float, nullable=True)
    prediction_top3_mean_finish: Mapped[float | None] = mapped_column(Float, nullable=True)
    relative_finish_error: Mapped[float | None] = mapped_column(Float, nullable=True)
    evaluation_variant: Mapped[str | None] = mapped_column(String, nullable=True)
    metrics_json: Mapped[dict] = mapped_column(JSON)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class TrainingRun(Base):
    __tablename__ = "training_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_version: Mapped[str] = mapped_column(String, index=True)
    trained_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    training_start: Mapped[str | None] = mapped_column(String, nullable=True)
    training_end: Mapped[str | None] = mapped_column(String, nullable=True)
    feature_version: Mapped[str | None] = mapped_column(String, nullable=True)
    label: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    race_count: Mapped[int] = mapped_column(Integer, default=0)
    metrics_json: Mapped[dict] = mapped_column(JSON)


class QualitativeEvidence(Base):
    __tablename__ = "qualitative_evidence"
    __table_args__ = (
        UniqueConstraint("content_hash", "race_id", "horse_id", name="uq_evidence_content_race_horse"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_type: Mapped[str] = mapped_column(String, index=True)
    source_name: Mapped[str] = mapped_column(String)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    author: Mapped[str | None] = mapped_column(String, nullable=True)
    race_id: Mapped[str] = mapped_column(ForeignKey("races.race_id"), index=True)
    horse_id: Mapped[str] = mapped_column(ForeignKey("horses.horse_id"), index=True)
    raw_text: Mapped[str] = mapped_column(Text)
    normalized_text: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    content_hash: Mapped[str] = mapped_column(String, index=True)
    collected_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


class SourceReliability(Base):
    __tablename__ = "source_reliability"
    __table_args__ = (
        UniqueConstraint("source_type", "source_name", name="uq_source_type_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_type: Mapped[str] = mapped_column(String)
    source_name: Mapped[str] = mapped_column(String)
    reliability: Mapped[float | None] = mapped_column(Float, nullable=True)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    metrics_json: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
