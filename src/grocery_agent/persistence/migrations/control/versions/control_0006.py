"""Private recipe bookmarks, condiment inventory and shopping preferences."""

import sqlalchemy as sa
from alembic import op

from grocery_agent.persistence.types import UTCDateTime

revision = "control_0006"
down_revision = "control_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "account_state",
        sa.Column("pantry_condiments", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.add_column("account_state", sa.Column("preferred_retailers", sa.JSON(none_as_null=True)))
    op.add_column("account_state", sa.Column("preferred_max_stores", sa.Integer()))
    op.create_table(
        "saved_recipes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("meal_index", sa.Integer(), nullable=False),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.UniqueConstraint("user_id", "job_id", "meal_index"),
    )
    op.create_index("ix_saved_recipes_user_id", "saved_recipes", ["user_id"])


def downgrade() -> None:
    raise ValueError("control downgrade deletes account history; restore a reviewed backup")
