"""Read a complete acquisition batch without leaking SQL into consumers."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import Engine, select
from sqlalchemy.orm import sessionmaker

from grocery_agent.models.offer import Offer
from grocery_agent.persistence.schema import (
    ObservationRow,
    ScrapeItemRow,
    ScrapeRunRow,
    SnapshotRow,
)


@dataclass(frozen=True)
class OfferBatch:
    run_id: str
    source_id: str
    finished_at: datetime


@dataclass(frozen=True)
class ObservedOffer:
    offer: Offer
    observed_at: datetime


class CurrentOfferReader(Protocol):
    def latest_batch(self, source_id: str) -> OfferBatch: ...

    def iter_offers(self, batch: OfferBatch) -> Iterator[ObservedOffer]: ...


class SQLAlchemyCurrentOfferReader:
    def __init__(self, engine: Engine) -> None:
        self.sessions = sessionmaker(engine)

    def latest_batch(self, source_id: str) -> OfferBatch:
        with self.sessions() as session:
            row = session.scalar(
                select(ScrapeRunRow)
                .where(ScrapeRunRow.source_id == source_id)
                .order_by(ScrapeRunRow.started_at.desc(), ScrapeRunRow.id)
                .limit(1)
            )
            if row is None or row.status != "success" or row.finished_at is None:
                raise ValueError("latest source run is missing, incomplete, or unsuccessful")
            return OfferBatch(row.id, row.source_id, row.finished_at)

    def iter_offers(self, batch: OfferBatch) -> Iterator[ObservedOffer]:
        # Membership in this run excludes disappeared offers and preserves the state actually
        # observed in the batch, even if a subsequent incomplete run changes the latest version.
        statement = (
            select(ObservationRow.payload, SnapshotRow.fetched_at)
            .join(ScrapeItemRow, ScrapeItemRow.observation_id == ObservationRow.id)
            .join(SnapshotRow, SnapshotRow.id == ScrapeItemRow.snapshot_id)
            .where(ScrapeItemRow.run_id == batch.run_id)
            .execution_options(yield_per=200)
        )
        with self.sessions() as session:
            for payload, observed_at in session.execute(statement):
                if not isinstance(observed_at, datetime):
                    raise ValueError("observation has no valid timestamp")
                yield ObservedOffer(Offer.model_validate(payload), observed_at)
