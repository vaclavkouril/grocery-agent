"""Profile-isolated collections; old runs remain explicitly unknown legacy coverage."""

import sqlalchemy as sa
from alembic import op

revision = "offers_0003"
down_revision = "offers_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    profiles = op.create_table(
        "catalogue_run_profiles",
        sa.Column("run_id", sa.String(36), sa.ForeignKey("scrape_runs.id"), primary_key=True),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("profile_fingerprint", sa.String(64), nullable=False),
        sa.Column("profile", sa.JSON()),
        sa.Column("coverage", sa.JSON(), nullable=False),
    )
    op.create_index("ix_catalogue_run_profiles_source_id", "catalogue_run_profiles", ["source_id"])
    op.create_index(
        "ix_catalogue_run_profiles_profile_fingerprint",
        "catalogue_run_profiles",
        ["profile_fingerprint"],
    )
    heads = op.create_table(
        "catalogue_profile_heads",
        sa.Column("source_id", sa.String(64), primary_key=True),
        sa.Column("profile_fingerprint", sa.String(64), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("scrape_runs.id"), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=False),
    )
    connection = op.get_bind()
    runs = sa.table("scrape_runs", sa.column("id", sa.String()), sa.column("store_id", sa.String()))
    # Stream historical runs rather than loading a price-history database into memory.
    for row in connection.execute(sa.select(runs)).mappings():
        connection.execute(
            profiles.insert().values(
                run_id=row["id"],
                source_id=row["store_id"],
                profile_fingerprint="legacy",
                profile=None,
                coverage={
                    "profile_fingerprint": "legacy",
                    "observed": 0,
                    "complete": False,
                    "actual_scopes": [],
                    "warnings": ["Historical acquisition coverage is unknown."],
                },
            )
        )
    old_heads = sa.table(
        "catalogue_heads",
        sa.column("source_id", sa.String()),
        sa.column("run_id", sa.String()),
        sa.column("published_at", sa.DateTime()),
    )
    connection.execute(
        heads.insert().from_select(
            ["source_id", "profile_fingerprint", "run_id", "published_at"],
            sa.select(
                old_heads.c.source_id,
                sa.literal("legacy"),
                old_heads.c.run_id,
                old_heads.c.published_at,
            ),
        )
    )


def downgrade() -> None:
    raise ValueError("profile downgrade loses collection identity; restore a reviewed backup")
