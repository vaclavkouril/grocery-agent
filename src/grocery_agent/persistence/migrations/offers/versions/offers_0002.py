"""Disposable indexed catalogue cache and atomic publication pointers."""

import sqlalchemy as sa
from alembic import op

revision = "offers_0002"
down_revision = "offers_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "catalogue_heads",
        sa.Column("source_id", sa.String(64), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("scrape_runs.id"), nullable=False),
        sa.Column("published_at", sa.DateTime, nullable=False),
    )
    op.create_table(
        "catalogue_entries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("scrape_runs.id"), nullable=False),
        sa.Column(
            "observation_id", sa.String(36), sa.ForeignKey("price_observations.id"), nullable=False
        ),
        sa.Column("scope", sa.String(500), nullable=False),
        sa.Column("retailer_id", sa.String(64), nullable=False),
        sa.Column("normalized_name", sa.String(500), nullable=False),
        sa.Column("category", sa.String(500)),
        sa.Column("unit", sa.String(24), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("unit_price_units", sa.BigInteger, nullable=False),
        sa.Column("requires_loyalty", sa.Boolean, nullable=False),
        sa.Column("price_qualifier", sa.String(24), nullable=False),
        sa.Column("availability", sa.String(24), nullable=False),
        sa.Column("observed_at", sa.DateTime, nullable=False),
        sa.Column("valid_from", sa.Date),
        sa.Column("valid_until", sa.Date),
        sa.UniqueConstraint("run_id", "observation_id"),
    )
    op.create_index("ix_catalogue_entries_run_id", "catalogue_entries", ["run_id"])
    op.create_index(
        "ix_catalogue_unit_price",
        "catalogue_entries",
        ["run_id", "unit", "currency", "unit_price_units"],
    )


def downgrade() -> None:
    op.drop_table("catalogue_entries")
    op.drop_table("catalogue_heads")
