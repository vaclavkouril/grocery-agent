"""Optional user identity and private parameter profiles."""

from alembic import op

from grocery_agent.persistence.migrations.baselines import control_baseline

revision = "control_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    control_baseline().create_all(op.get_bind(), checkfirst=False)


def downgrade() -> None:
    raise ValueError("baseline downgrade deletes private profiles; restore a reviewed backup")
