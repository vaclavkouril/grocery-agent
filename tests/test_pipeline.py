import asyncio
import copy
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from grocery_agent.matching.base import ExactGTINResolver
from grocery_agent.persistence.repository import SQLAlchemyOfferRepository
from grocery_agent.persistence.schema import ObservationRow, ScrapeItemRow
from grocery_agent.persistence.snapshots import FileSnapshotStore
from grocery_agent.pipeline.service import ScrapePipeline
from grocery_agent.stores.base import AcquisitionItem, AdapterContext, SourceEvidence, StoreAdapter


class FixtureAdapter(StoreAdapter):
    def __init__(self, items: list[AcquisitionItem], error: BaseException | None = None) -> None:
        self.items = items
        self.error = error

    @property
    def store_id(self) -> str:
        return "mock"

    async def fetch_offers(self, context: AdapterContext) -> AsyncIterator[AcquisitionItem]:
        for item in self.items:
            yield item
        if self.error:
            raise self.error


def make_item(candidate: dict[str, Any], at: datetime | None = None) -> AcquisitionItem:
    evidence = SourceEvidence(
        json.dumps(candidate).encode(),
        "https://example.invalid",
        "application/json",
        at or datetime.now(UTC),
        "$",
    )
    return AcquisitionItem(evidence=evidence, candidate=copy.deepcopy(candidate))


async def run_fixture(
    repository: SQLAlchemyOfferRepository, snapshots: FileSnapshotStore, adapter: StoreAdapter
):
    pipeline = ScrapePipeline(repository, snapshots, ExactGTINResolver())
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200))
    ) as http:
        return await pipeline.run(adapter, AdapterContext(http))


async def test_rejected_items_do_not_corrupt_database(
    repository: SQLAlchemyOfferRepository,
    snapshots: FileSnapshotStore,
    candidate: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    good = make_item(candidate)
    candidate["current_price"] = "-1"
    invalid = make_item(candidate)
    parser_error = AcquisitionItem(evidence=invalid.evidence, error="missing price")
    result = await run_fixture(repository, snapshots, FixtureAdapter([invalid, parser_error, good]))
    assert (result.fetched, result.accepted, result.rejected, result.changed) == (3, 1, 2, 1)
    assert result.status == "partial" and result.errors == 0
    with repository.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ObservationRow)) == 1
        rejected = list(
            session.scalars(select(ScrapeItemRow).where(ScrapeItemRow.status == "rejected"))
        )
        assert len(rejected) == 2 and all(row.details for row in rejected)
    assert len([r for r in caplog.records if r.message == "item_rejected"]) == 2


async def test_store_identity_mismatch_rejected(
    repository: SQLAlchemyOfferRepository, snapshots: FileSnapshotStore, candidate: dict[str, Any]
) -> None:
    candidate["product"]["store_id"] = "another"
    result = await run_fixture(repository, snapshots, FixtureAdapter([make_item(candidate)]))
    assert result.rejected == 1 and result.accepted == 0


async def test_source_failure_preserves_accepted_items(
    repository: SQLAlchemyOfferRepository, snapshots: FileSnapshotStore, candidate: dict[str, Any]
) -> None:
    result = await run_fixture(
        repository,
        snapshots,
        FixtureAdapter([make_item(candidate)], httpx.ReadTimeout("source timed out")),
    )
    assert result.status == "failed" and result.errors == 1 and result.accepted == 1
    assert "ReadTimeout" in result.error_details[0]
    assert repository.recent_runs(1)[0]["errors"] == 1


async def test_empty_scrape_is_observable(
    repository: SQLAlchemyOfferRepository, snapshots: FileSnapshotStore
) -> None:
    result = await run_fixture(repository, snapshots, FixtureAdapter([]))
    assert result.status == "empty" and result.errors == 1 and result.fetched == 0
    assert repository.recent_runs(1)[0]["status"] == "empty"


async def test_cancelled_run_is_finalized(
    repository: SQLAlchemyOfferRepository, snapshots: FileSnapshotStore
) -> None:
    with pytest.raises(asyncio.CancelledError):
        await run_fixture(repository, snapshots, FixtureAdapter([], asyncio.CancelledError()))
    run = repository.recent_runs(1)[0]
    assert run["status"] == "cancelled" and run["finished_at"] is not None


async def test_older_observation_is_rejected(
    repository: SQLAlchemyOfferRepository, snapshots: FileSnapshotStore, candidate: dict[str, Any]
) -> None:
    at = datetime.now(UTC)
    first, older = make_item(candidate, at), make_item(candidate, at - timedelta(days=1))
    result = await run_fixture(repository, snapshots, FixtureAdapter([first, older]))
    assert result.accepted == 1 and result.rejected == 1


async def test_pipeline_persists_before_requesting_next_item(
    repository: SQLAlchemyOfferRepository, snapshots: FileSnapshotStore, candidate: dict[str, Any]
) -> None:
    class StreamingAdapter(FixtureAdapter):
        async def fetch_offers(self, context: AdapterContext) -> AsyncIterator[AcquisitionItem]:
            for index in range(10):
                with repository.sessions() as session:
                    assert session.scalar(select(func.count()).select_from(ScrapeItemRow)) == index
                yield make_item(candidate)

    result = await run_fixture(repository, snapshots, StreamingAdapter([]))
    assert result.accepted == 10 and result.changed == 1


async def test_storage_failure_is_run_error(
    repository: SQLAlchemyOfferRepository,
    snapshots: FileSnapshotStore,
    candidate: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(evidence: SourceEvidence) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(snapshots, "save", fail)
    result = await run_fixture(repository, snapshots, FixtureAdapter([make_item(candidate)]))
    assert result.status == "failed" and result.errors == 1 and result.accepted == 0
    assert result.rejected == 0


async def test_run_logs_have_counts_and_ids(
    repository: SQLAlchemyOfferRepository,
    snapshots: FileSnapshotStore,
    candidate: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    result = await run_fixture(repository, snapshots, FixtureAdapter([make_item(candidate)]))
    event = next(r for r in caplog.records if r.message == "scrape_finished")
    assert event.fields["run_id"] == result.run_id and event.fields["store_id"] == "mock"
    assert event.fields["accepted"] == 1 and event.fields["finished_at"]


def test_acquisition_item_requires_candidate_or_error(candidate: dict[str, Any]) -> None:
    item = make_item(candidate)
    with pytest.raises(ValueError):
        AcquisitionItem(evidence=item.evidence)
    with pytest.raises(ValueError):
        replace(item, error="bad")
