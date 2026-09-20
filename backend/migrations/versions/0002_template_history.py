"""Preserve editable prompt versions without losing existing template records."""

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("templates", sa.Column("history", sa.JSON(), nullable=False, server_default="[]"))


def downgrade():
    # Export/backup the workspace before downgrade: removing this column loses prompt-version history.
    with op.batch_alter_table("templates") as batch:
        batch.drop_column("history")
