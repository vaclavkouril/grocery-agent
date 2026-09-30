from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    """UTC storage for SQLite/PostgreSQL; naive domain timestamps are rejected."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must have a timezone")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return value.replace(tzinfo=UTC) if value is not None else None


class Base(DeclarativeBase):
    pass


class ProductRow(Base):
    __tablename__ = "products"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    gtin: Mapped[str | None] = mapped_column(String(14), unique=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON)


class StoreProductRow(Base):
    __tablename__ = "store_products"
    __table_args__ = (UniqueConstraint("store_id", "sku"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    store_id: Mapped[str] = mapped_column(String(64), index=True)
    sku: Mapped[str] = mapped_column(String(500))
    product_id: Mapped[str | None] = mapped_column(ForeignKey("products.id"))
    details: Mapped[dict[str, Any]] = mapped_column(JSON)


class OfferRow(Base):
    __tablename__ = "offers"
    __table_args__ = (UniqueConstraint("store_product_id", "offer_key", "scope"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    store_product_id: Mapped[str] = mapped_column(ForeignKey("store_products.id"), index=True)
    offer_key: Mapped[str] = mapped_column(String(500))
    scope: Mapped[str] = mapped_column(String(500))
    version: Mapped[int] = mapped_column(Integer, default=0)


class ScrapeRunRow(Base):
    __tablename__ = "scrape_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # Retain the original physical column for existing SQLite databases.
    source_id: Mapped[str] = mapped_column("store_id", String(64), index=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(String(24))
    fetched: Mapped[int] = mapped_column(Integer, default=0)
    accepted: Mapped[int] = mapped_column(Integer, default=0)
    rejected: Mapped[int] = mapped_column(Integer, default=0)
    changed: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    error_details: Mapped[list[str]] = mapped_column(JSON, default=list)


class SnapshotRow(Base):
    __tablename__ = "source_snapshots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("scrape_runs.id"), index=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    relative_path: Mapped[str] = mapped_column(String(128))
    url: Mapped[str] = mapped_column(Text)
    media_type: Mapped[str] = mapped_column(String(128))
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime)
    locator: Mapped[str] = mapped_column(Text)
    source_metadata: Mapped[dict[str, Any]] = mapped_column(JSON)


class ObservationRow(Base):
    __tablename__ = "price_observations"
    __table_args__ = (UniqueConstraint("offer_id", "version"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    offer_id: Mapped[str] = mapped_column(ForeignKey("offers.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    fingerprint: Mapped[str] = mapped_column(String(64))
    current_price_units: Mapped[int] = mapped_column(BigInteger)
    regular_price_units: Mapped[int | None] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("source_snapshots.id"))


class ScrapeItemRow(Base):
    __tablename__ = "scrape_items"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("scrape_runs.id"), index=True)
    snapshot_id: Mapped[str] = mapped_column(ForeignKey("source_snapshots.id"))
    observation_id: Mapped[str | None] = mapped_column(ForeignKey("price_observations.id"))
    status: Mapped[str] = mapped_column(String(24))
    details: Mapped[str | None] = mapped_column(Text)
