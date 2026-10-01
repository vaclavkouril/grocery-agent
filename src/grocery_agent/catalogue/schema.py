from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from grocery_agent.persistence.schema import Base
from grocery_agent.persistence.types import UTCDateTime


class CatalogueHeadRow(Base):
    __tablename__ = "catalogue_heads"
    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("scrape_runs.id"))
    published_at: Mapped[datetime] = mapped_column(UTCDateTime)


class CatalogueEntryRow(Base):
    __tablename__ = "catalogue_entries"
    __table_args__ = (
        UniqueConstraint("run_id", "observation_id"),
        Index("ix_catalogue_unit_price", "run_id", "unit", "currency", "unit_price_units"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("scrape_runs.id"), index=True)
    observation_id: Mapped[str] = mapped_column(ForeignKey("price_observations.id"))
    scope: Mapped[str] = mapped_column(String(500))
    retailer_id: Mapped[str] = mapped_column(String(64))
    normalized_name: Mapped[str] = mapped_column(String(500))
    category: Mapped[str | None] = mapped_column(String(500))
    unit: Mapped[str] = mapped_column(String(24))
    currency: Mapped[str] = mapped_column(String(3))
    unit_price_units: Mapped[int] = mapped_column(BigInteger)
    requires_loyalty: Mapped[bool] = mapped_column(Boolean)
    price_qualifier: Mapped[str] = mapped_column(String(24))
    availability: Mapped[str] = mapped_column(String(24))
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime)
    valid_from: Mapped[date | None] = mapped_column(Date)
    valid_until: Mapped[date | None] = mapped_column(Date)
