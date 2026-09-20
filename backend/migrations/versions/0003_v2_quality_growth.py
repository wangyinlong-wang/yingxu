"""Add V2 production quality and growth-loop records."""

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def _record_columns():
    return (
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
    )


def upgrade():
    op.add_column(
        "contents",
        sa.Column(
            "preset_id",
            sa.String(length=100),
            nullable=False,
            server_default="tech_brief_video_v1",
        ),
    )
    op.add_column(
        "contents",
        sa.Column("brief", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.add_column(
        "approvals",
        sa.Column("compliance", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )

    op.create_table(
        "quality_reviews",
        sa.Column("content_id", sa.String(length=36), nullable=False),
        sa.Column("content_version", sa.Integer(), nullable=False),
        sa.Column("video_asset_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("report", sa.JSON(), nullable=False),
        *_record_columns(),
        sa.ForeignKeyConstraint(["content_id"], ["contents.id"]),
        sa.ForeignKeyConstraint(["video_asset_id"], ["assets.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "content_id",
            "content_version",
            "video_asset_id",
            name="uq_quality_reviews_content_version_video",
        ),
    )
    op.create_index("ix_quality_reviews_content_id", "quality_reviews", ["content_id"])

    op.create_table(
        "demand_signals",
        sa.Column("title", sa.String(length=1000), nullable=False),
        sa.Column("source_kind", sa.String(length=20), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("scores", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        *_record_columns(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_demand_signals_status", "demand_signals", ["status"])

    op.create_table(
        "benchmarks",
        sa.Column("platform", sa.String(length=20), nullable=False),
        sa.Column("account_name", sa.String(length=200), nullable=False),
        sa.Column("content_url", sa.Text(), nullable=True),
        sa.Column("title", sa.String(length=1000), nullable=False),
        sa.Column("content_type", sa.String(length=20), nullable=False),
        sa.Column("analysis", sa.JSON(), nullable=False),
        *_record_columns(),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "experiments",
        sa.Column("name", sa.String(length=500), nullable=False),
        sa.Column("hypothesis", sa.Text(), nullable=False),
        sa.Column("variable", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("content_ids", sa.JSON(), nullable=False),
        sa.Column("publication_ids", sa.JSON(), nullable=False),
        sa.Column("observation_windows", sa.JSON(), nullable=False),
        sa.Column("conclusion", sa.Text(), nullable=False),
        sa.Column("next_action", sa.String(length=30), nullable=False),
        *_record_columns(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_experiments_status", "experiments", ["status"])


def downgrade():
    # This removes V2 strategy, review, brief and compliance data. Prefer restoring
    # the verified pre-upgrade workspace backup instead of downgrading in place.
    op.drop_index("ix_experiments_status", table_name="experiments")
    op.drop_table("experiments")
    op.drop_table("benchmarks")
    op.drop_index("ix_demand_signals_status", table_name="demand_signals")
    op.drop_table("demand_signals")
    op.drop_index("ix_quality_reviews_content_id", table_name="quality_reviews")
    op.drop_table("quality_reviews")
    with op.batch_alter_table("approvals") as batch:
        batch.drop_column("compliance")
    with op.batch_alter_table("contents") as batch:
        batch.drop_column("brief")
        batch.drop_column("preset_id")
