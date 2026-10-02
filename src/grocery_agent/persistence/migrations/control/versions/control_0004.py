"""Cookie CSRF, bounded durable authentication throttles and refresh receipts."""

import sqlalchemy as sa
from alembic import op

revision = "control_0004"
down_revision = "control_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("csrf_digest", sa.String(64)))
    op.create_table(
        "auth_rate_limits",
        sa.Column("bucket", sa.String(64), primary_key=True),
        sa.Column("window_start", sa.DateTime(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
    )
    op.create_table(
        "refresh_gates",
        sa.Column("identity", sa.String(64), primary_key=True),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("jobs.id")),
        sa.Column("cooldown_until", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "refresh_receipts",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("idempotency_key", sa.String(200), primary_key=True),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("request", sa.JSON(), nullable=False),
    )


def downgrade() -> None:
    raise ValueError("control downgrade deletes authentication state; restore a reviewed backup")
