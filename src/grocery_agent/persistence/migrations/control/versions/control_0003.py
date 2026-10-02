"""Durable channel intake, confirmations and fenced notification delivery."""

import sqlalchemy as sa
from alembic import op

revision = "control_0003"
down_revision = "control_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("notification_outbox") as batch:
        batch.alter_column("job_id", existing_type=sa.String(36), nullable=True)
        batch.add_column(sa.Column("payload", sa.JSON()))
        batch.add_column(sa.Column("lease_token", sa.String(64)))
        batch.add_column(sa.Column("lease_until", sa.DateTime()))
        batch.add_column(sa.Column("available_at", sa.DateTime()))
        batch.add_column(sa.Column("error_code", sa.String(64)))
    op.create_table(
        "channel_events",
        sa.Column("channel", sa.String(24), primary_key=True),
        sa.Column("event_id", sa.String(200), primary_key=True),
        sa.Column("address", sa.String(320), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("response", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "channel_confirmations",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column(
            "binding_id", sa.String(36), sa.ForeignKey("channel_bindings.id"), nullable=False
        ),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime()),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("jobs.id")),
    )
    op.create_index("ix_channel_confirmations_binding_id", "channel_confirmations", ["binding_id"])


def downgrade() -> None:
    raise ValueError("control downgrade deletes channel state; restore a reviewed backup")
