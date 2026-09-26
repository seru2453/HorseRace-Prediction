"""Add champion/challenger learning foundation without replacing existing records."""

from alembic import op
import sqlalchemy as sa


revision = "20260926_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "races" not in existing_tables:
        from database import models  # noqa: F401
        from database.session import Base

        Base.metadata.create_all(op.get_bind())
        return
    with op.batch_alter_table("prediction_metrics") as batch:
        batch.add_column(sa.Column("current_form", sa.Float(), nullable=True))
        batch.add_column(sa.Column("condition_fit", sa.Float(), nullable=True))
        batch.add_column(sa.Column("previous_race_residual", sa.Float(), nullable=True))
        batch.add_column(sa.Column("rebound_score", sa.Float(), nullable=True))
        batch.add_column(sa.Column("field_size", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("jev_features", sa.JSON(), nullable=True))

    with op.batch_alter_table("race_result_entries") as batch:
        batch.add_column(sa.Column("field_size", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("relative_finish", sa.Float(), nullable=True))
        batch.add_column(sa.Column("relative_finish_category", sa.String(), nullable=True))

    with op.batch_alter_table("model_evaluations") as batch:
        batch.add_column(sa.Column("horse_count", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("training_run_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("training_start", sa.String(), nullable=True))
        batch.add_column(sa.Column("training_end", sa.String(), nullable=True))
        batch.add_column(sa.Column("evaluation_start", sa.String(), nullable=True))
        batch.add_column(sa.Column("evaluation_end", sa.String(), nullable=True))
        batch.add_column(sa.Column("top3_recall", sa.Float(), nullable=True))
        batch.add_column(sa.Column("winner_rank_mean", sa.Float(), nullable=True))
        batch.add_column(sa.Column("prediction_top3_mean_finish", sa.Float(), nullable=True))
        batch.add_column(sa.Column("relative_finish_error", sa.Float(), nullable=True))
        batch.add_column(sa.Column("evaluation_variant", sa.String(), nullable=True))
        batch.create_foreign_key(
            "fk_model_evaluations_training_run_id",
            "training_runs",
            ["training_run_id"],
            ["id"],
        )
        batch.create_index("ix_model_evaluations_training_run_id", ["training_run_id"])

    op.create_table(
        "challenger_predictions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("prediction_snapshot_id", sa.Integer(), sa.ForeignKey("prediction_snapshots.id"), nullable=False),
        sa.Column("model_version", sa.String(), nullable=False),
        sa.Column("feature_version", sa.String(), nullable=True),
        sa.Column("target", sa.String(), nullable=False),
        sa.Column("predicted_value", sa.Float(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("prediction_snapshot_id", "model_version", "target", name="uq_challenger_snapshot_model_target"),
    )
    op.create_index("ix_challenger_predictions_prediction_snapshot_id", "challenger_predictions", ["prediction_snapshot_id"])
    op.create_index("ix_challenger_predictions_model_version", "challenger_predictions", ["model_version"])
    op.create_index("ix_challenger_predictions_target", "challenger_predictions", ["target"])

    op.create_table(
        "qualitative_evidence",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source_type", sa.String(), nullable=False),
        sa.Column("source_name", sa.String(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("author", sa.String(), nullable=True),
        sa.Column("race_id", sa.String(), sa.ForeignKey("races.race_id"), nullable=False),
        sa.Column("horse_id", sa.String(), sa.ForeignKey("horses.horse_id"), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("normalized_text", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("collected_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("content_hash", "race_id", "horse_id", name="uq_evidence_content_race_horse"),
    )
    op.create_index("ix_qualitative_evidence_race_id", "qualitative_evidence", ["race_id"])
    op.create_index("ix_qualitative_evidence_horse_id", "qualitative_evidence", ["horse_id"])
    op.create_index("ix_qualitative_evidence_published_at", "qualitative_evidence", ["published_at"])
    op.create_index("ix_qualitative_evidence_content_hash", "qualitative_evidence", ["content_hash"])
    op.create_index("ix_qualitative_evidence_source_type", "qualitative_evidence", ["source_type"])

    op.create_table(
        "source_reliability",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source_type", sa.String(), nullable=False),
        sa.Column("source_name", sa.String(), nullable=False),
        sa.Column("reliability", sa.Float(), nullable=True),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("metrics_json", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("source_type", "source_name", name="uq_source_type_name"),
    )


def downgrade() -> None:
    raise RuntimeError("This migration is intentionally non-destructive; restore a backup instead.")
