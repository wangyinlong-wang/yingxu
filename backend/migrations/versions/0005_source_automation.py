"""Source health, focus keywords, topic clustering and interval-based collection schedules."""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sources", sa.Column("keywords", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
    op.add_column("sources", sa.Column("options", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
    op.add_column(
        "sources", sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("sources", sa.Column("last_success_at", sa.String(40), nullable=True))
    op.add_column("sources", sa.Column("next_eligible_at", sa.String(40), nullable=True))
    op.add_column("topics", sa.Column("cluster_size", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("topics", sa.Column("related", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
    op.add_column("schedules", sa.Column("interval_minutes", sa.Integer(), nullable=True))
    op.add_column(
        "schedules", sa.Column("include_paid", sa.Boolean(), nullable=False, server_default=sa.false())
    )


def downgrade():
    # Old workers reject the new source kinds and would treat a collect schedule as a metrics run.
    connection = op.get_bind()
    new_sources = connection.execute(
        sa.text("SELECT COUNT(*) FROM sources WHERE kind IN ('x_tikhub_search', 'html_list')")
    ).scalar()
    collect_schedules = connection.execute(
        sa.text("SELECT COUNT(*) FROM schedules WHERE kind = 'collect'")
    ).scalar()
    if new_sources or collect_schedules:
        raise RuntimeError(
            "New source kinds or collect schedules exist; restore a verified pre-upgrade backup instead."
        )
    with op.batch_alter_table("schedules") as batch:
        batch.drop_column("include_paid")
        batch.drop_column("interval_minutes")
    with op.batch_alter_table("topics") as batch:
        batch.drop_column("related")
        batch.drop_column("cluster_size")
    with op.batch_alter_table("sources") as batch:
        for column in ("next_eligible_at", "last_success_at", "consecutive_failures", "options", "keywords"):
            batch.drop_column(column)
