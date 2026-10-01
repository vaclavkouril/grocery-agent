"""Baseline canonical offer and evidence schema."""

from alembic import op

from grocery_agent.persistence.migrations.baselines import offer_baseline

revision = "offers_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    offer_baseline().create_all(op.get_bind(), checkfirst=False)


def downgrade() -> None:
    raise ValueError("baseline downgrade deletes history; restore a reviewed database backup")
