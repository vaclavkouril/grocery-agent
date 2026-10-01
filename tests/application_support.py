import json
from datetime import UTC, datetime

from grocery_agent.matching.base import ExactGTINResolver
from grocery_agent.models.observation import PriceObservation
from grocery_agent.models.offer import Offer
from grocery_agent.persistence.repository import SQLAlchemyOfferRepository
from grocery_agent.persistence.snapshots import FileSnapshotStore
from grocery_agent.pipeline.results import ScrapeResult
from grocery_agent.stores.base import SourceEvidence

NOW = datetime(2026, 10, 1, 8, tzinfo=UTC)


def complete_batch(
    repository: SQLAlchemyOfferRepository,
    snapshots: FileSnapshotStore,
    offers: list[Offer],
    at: datetime = NOW,
    source: str = "mock",
    observed_at: datetime | None = None,
) -> ScrapeResult:
    run = ScrapeResult(source_id=source, started_at=at)
    repository.start_run(run)
    for offer in offers:
        payload = offer.model_dump(mode="json", exclude_computed_fields=True)
        evidence = SourceEvidence(
            json.dumps(payload).encode(),
            str(offer.source_url),
            "application/json",
            observed_at or at,
            offer.product.sku,
        )
        snapshot = snapshots.save(evidence)
        repository.add_snapshot(run.run_id, snapshot)
        run.changed += int(
            repository.accept(
                run.run_id,
                PriceObservation(
                    offer=offer, observed_at=evidence.fetched_at, snapshot_id=snapshot.id
                ),
                ExactGTINResolver().resolve(offer.product),
            )
        )
        run.accepted += 1
        run.fetched += 1
    run.status, run.finished_at = "success", at
    repository.finish_run(run)
    return run


def failed_batch(
    repository: SQLAlchemyOfferRepository, at: datetime, status: str = "failed"
) -> ScrapeResult:
    run = ScrapeResult(source_id="mock", started_at=at, status="running")
    repository.start_run(run)
    run.status, run.finished_at, run.errors = status, at, 1
    repository.finish_run(run)
    return run
