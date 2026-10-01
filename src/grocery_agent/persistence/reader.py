"""Read a complete acquisition batch without leaking SQL into consumers."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import Engine, func, select
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
    warnings: tuple[str, ...] = ()
    latest_run_status: str | None = None


@dataclass(frozen=True)
class ObservedOffer:
    offer: Offer
    observed_at: datetime
    observation_id: str | None = None


class CurrentOfferReader(Protocol):
    def latest_batch(self, source_id: str) -> OfferBatch: ...

    def iter_offers(self, batch: OfferBatch) -> Iterator[ObservedOffer]: ...


class BatchOfferReader(CurrentOfferReader, Protocol):
    def get_batch(self, run_id: str, source_id: str) -> OfferBatch: ...


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

    def get_batch(self, run_id: str, source_id: str) -> OfferBatch:
        with self.sessions() as session:
            row = session.get(ScrapeRunRow, run_id)
            if (
                row is None
                or row.source_id != source_id
                or row.status != "success"
                or row.finished_at is None
            ):
                raise ValueError("selected batch is missing, unsuccessful, or from another source")
            return OfferBatch(row.id, row.source_id, row.finished_at)

    def iter_offers(self, batch: OfferBatch) -> Iterator[ObservedOffer]:
        # Membership in this run excludes disappeared offers and preserves the state actually
        # observed in the batch, even if a subsequent incomplete run changes the latest version.
        versions = (
            select(ObservationRow.offer_id, func.max(ObservationRow.version).label("version"))
            .join(ScrapeItemRow, ScrapeItemRow.observation_id == ObservationRow.id)
            .where(ScrapeItemRow.run_id == batch.run_id)
            .group_by(ObservationRow.offer_id)
            .subquery()
        )
        times = (
            select(
                ScrapeItemRow.observation_id, func.max(SnapshotRow.fetched_at).label("observed_at")
            )
            .join(SnapshotRow, SnapshotRow.id == ScrapeItemRow.snapshot_id)
            .where(ScrapeItemRow.run_id == batch.run_id)
            .group_by(ScrapeItemRow.observation_id)
            .subquery()
        )
        statement = (
            select(ObservationRow.payload, times.c.observed_at, ObservationRow.id)
            .join(
                versions,
                (versions.c.offer_id == ObservationRow.offer_id)
                & (versions.c.version == ObservationRow.version),
            )
            .join(times, times.c.observation_id == ObservationRow.id)
            .execution_options(yield_per=200)
        )
        with self.sessions() as session:
            for payload, observed_at, observation_id in session.execute(statement):
                if not isinstance(observed_at, datetime) or not isinstance(observation_id, str):
                    raise ValueError("observation has no valid timestamp")
                yield ObservedOffer(Offer.model_validate(payload), observed_at, observation_id)
