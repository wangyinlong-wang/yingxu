"""Add local browser publishing while preserving existing Android accounts."""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("accounts", sa.Column("channel", sa.String(20), nullable=False, server_default="android"))


def downgrade():
    # Old workers would interpret browser reservations as ADB serials. Refuse an unsafe rollback.
    connection = op.get_bind()
    browser_accounts = connection.execute(sa.text("SELECT COUNT(*) FROM accounts WHERE channel != 'android'")).scalar()
    browser_publications = connection.execute(sa.text(
        "SELECT COUNT(*) FROM publications WHERE json_extract(snapshot, '$.account.channel') = 'browser'"
    )).scalar()
    if browser_accounts or browser_publications:
        raise RuntimeError("Browser accounts exist; restore a verified pre-upgrade backup instead.")
    with op.batch_alter_table("accounts") as batch:
        batch.drop_column("channel")
