"""Frozen initial schemas. Never replace these with mutable ORM metadata."""

import sqlalchemy as sa


def offer_baseline() -> sa.MetaData:
    m = sa.MetaData()
    sa.Table(
        "products",
        m,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("gtin", sa.String(14), unique=True),
        sa.Column("details", sa.JSON, nullable=False),
    )
    sa.Table(
        "store_products",
        m,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("store_id", sa.String(64), nullable=False, index=True),
        sa.Column("sku", sa.String(500), nullable=False),
        sa.Column("product_id", sa.String(36), sa.ForeignKey("products.id")),
        sa.Column("details", sa.JSON, nullable=False),
        sa.UniqueConstraint("store_id", "sku"),
    )
    sa.Table(
        "offers",
        m,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "store_product_id",
            sa.String(36),
            sa.ForeignKey("store_products.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("offer_key", sa.String(500), nullable=False),
        sa.Column("scope", sa.String(500), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.UniqueConstraint("store_product_id", "offer_key", "scope"),
    )
    sa.Table(
        "scrape_runs",
        m,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("store_id", sa.String(64), nullable=False, index=True),
        sa.Column("started_at", sa.DateTime, nullable=False, index=True),
        sa.Column("finished_at", sa.DateTime),
        sa.Column("status", sa.String(24), nullable=False),
        *(
            sa.Column(name, sa.Integer, nullable=False)
            for name in ("fetched", "accepted", "rejected", "changed", "errors")
        ),
        sa.Column("error_details", sa.JSON, nullable=False),
    )
    sa.Table(
        "source_snapshots",
        m,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "run_id", sa.String(36), sa.ForeignKey("scrape_runs.id"), nullable=False, index=True
        ),
        sa.Column("sha256", sa.String(64), nullable=False, index=True),
        sa.Column("relative_path", sa.String(128), nullable=False),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("media_type", sa.String(128), nullable=False),
        sa.Column("fetched_at", sa.DateTime, nullable=False),
        sa.Column("locator", sa.Text, nullable=False),
        sa.Column("source_metadata", sa.JSON, nullable=False),
    )
    sa.Table(
        "price_observations",
        m,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "offer_id", sa.String(36), sa.ForeignKey("offers.id"), nullable=False, index=True
        ),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("current_price_units", sa.BigInteger, nullable=False),
        sa.Column("regular_price_units", sa.BigInteger),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("first_seen_at", sa.DateTime, nullable=False),
        sa.Column("last_seen_at", sa.DateTime, nullable=False),
        sa.Column(
            "snapshot_id", sa.String(36), sa.ForeignKey("source_snapshots.id"), nullable=False
        ),
        sa.UniqueConstraint("offer_id", "version"),
    )
    sa.Table(
        "scrape_items",
        m,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "run_id", sa.String(36), sa.ForeignKey("scrape_runs.id"), nullable=False, index=True
        ),
        sa.Column(
            "snapshot_id", sa.String(36), sa.ForeignKey("source_snapshots.id"), nullable=False
        ),
        sa.Column("observation_id", sa.String(36), sa.ForeignKey("price_observations.id")),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("details", sa.Text),
    )
    return m


def control_baseline() -> sa.MetaData:
    m = sa.MetaData()
    sa.Table(
        "users",
        m,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(512)),
        sa.Column("role", sa.String(24), nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    sa.Table(
        "user_profiles",
        m,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False),
        sa.Column("parameters", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint("user_id", "name"),
    )
    return m
