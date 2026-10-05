"""Revision-controlled account language preferences and pantry."""

import sqlalchemy as sa
from alembic import op

revision = "control_0005"
down_revision = "control_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "account_state",
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("settings_revision", sa.Integer(), nullable=False),
        sa.Column("ui_language", sa.String(2)),
        sa.Column("recipe_language", sa.String(2)),
        sa.Column("pantry_revision", sa.Integer(), nullable=False),
        sa.Column("pantry_items", sa.JSON(), nullable=False),
    )


def downgrade() -> None:
    raise ValueError("control downgrade deletes account state; restore a reviewed backup")
