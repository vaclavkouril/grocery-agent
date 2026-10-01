from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, func, select

from grocery_agent.catalogue.config import CatalogueSettings
from grocery_agent.catalogue.models import CatalogueQuery
from grocery_agent.catalogue.repository import PublishedOfferReader, SQLAlchemyCatalogueRepository
from grocery_agent.catalogue.schema import CatalogueEntryRow
from grocery_agent.models.offer import Offer
from grocery_agent.persistence.reader import SQLAlchemyCurrentOfferReader
from grocery_agent.persistence.repository import SQLAlchemyOfferRepository
from grocery_agent.persistence.schema import ObservationRow
from grocery_agent.persistence.snapshots import FileSnapshotStore
from tests.application_support import NOW, complete_batch, failed_batch


def variant(candidate: dict[str, Any], **changes: Any) -> Offer:
    return Offer.model_validate({**candidate, "regular_price": None, "promotion": None, **changes})


def test_publish_is_idempotent_and_replacement_keeps_history(
    engine: Engine,
    repository: SQLAlchemyOfferRepository,
    snapshots: FileSnapshotStore,
    candidate: dict[str, Any],
) -> None:
    first = complete_batch(repository, snapshots, [Offer.model_validate(candidate)])
    cache = SQLAlchemyCatalogueRepository(engine)
    cache.publish(first.run_id)
    cache.publish(first.run_id)
    second = complete_batch(
        repository,
        snapshots,
        [variant(candidate, current_price="18.9")],
        NOW + timedelta(minutes=1),
    )
    cache.publish(second.run_id)
    assert cache.state("mock", NOW + timedelta(minutes=1)).run_id == second.run_id
    with repository.sessions() as session:
        assert session.scalar(select(func.count()).select_from(CatalogueEntryRow)) == 1
        assert session.scalar(select(func.count()).select_from(ObservationRow)) == 2
    with pytest.raises(ValueError, match="older"):
        cache.publish(first.run_id)


@pytest.mark.parametrize("status", ["running", "failed", "partial", "empty", "cancelled"])
def test_failure_keeps_last_complete_with_visible_warning_and_deadline(
    engine: Engine,
    repository: SQLAlchemyOfferRepository,
    snapshots: FileSnapshotStore,
    candidate: dict[str, Any],
    status: str,
) -> None:
    run = complete_batch(repository, snapshots, [Offer.model_validate(candidate)])
    cache = SQLAlchemyCatalogueRepository(engine, CatalogueSettings(max_age_hours=36))
    cache.publish(run.run_id)
    failure = failed_batch(repository, NOW + timedelta(minutes=1), status)
    with pytest.raises(ValueError, match="complete"):
        cache.publish(failure.run_id)
    page = cache.search(CatalogueQuery(source_id="mock"), NOW + timedelta(hours=1))
    assert page.state.run_id == run.run_id and page.state.degraded
    assert page.state.latest_run_status == status and status in page.state.warnings[0]
    assert page.total == 1
    raw = SQLAlchemyCurrentOfferReader(engine)
    cached = PublishedOfferReader(raw, cache, lambda: NOW + timedelta(hours=1))
    assert cached.latest_batch("mock").warnings
    with pytest.raises(ValueError, match="unsuccessful"):
        raw.latest_batch("mock")
    with pytest.raises(ValueError, match="stale"):
        cache.search(CatalogueQuery(source_id="mock"), NOW + timedelta(hours=37))
    strict = SQLAlchemyCatalogueRepository(engine, CatalogueSettings(allow_cached_on_failure=False))
    with pytest.raises(ValueError, match="disabled"):
        strict.state("mock", NOW + timedelta(hours=1))


@pytest.mark.parametrize(
    "change",
    [
        {"valid_from": "2026-09-01", "valid_until": "2026-09-30"},
        {"valid_from": "2026-10-02", "valid_until": "2026-10-03"},
        {"availability": "unavailable"},
        {"price_qualifier": "from"},
        {"promotion": {"kind": "loyalty", "requires_loyalty": True}},
    ],
)
def test_default_catalogue_hides_expired_upcoming_and_conditional_offers(
    engine: Engine,
    repository: SQLAlchemyOfferRepository,
    snapshots: FileSnapshotStore,
    candidate: dict[str, Any],
    change: dict[str, Any],
) -> None:
    run = complete_batch(repository, snapshots, [variant(candidate, **change)])
    cache = SQLAlchemyCatalogueRepository(engine)
    cache.publish(run.run_id)
    assert cache.search(CatalogueQuery(source_id="mock"), NOW).total == 0


def test_individual_old_observations_are_not_freshened_by_publication(
    engine: Engine,
    repository: SQLAlchemyOfferRepository,
    snapshots: FileSnapshotStore,
    candidate: dict[str, Any],
) -> None:
    run = complete_batch(
        repository,
        snapshots,
        [Offer.model_validate(candidate)],
        observed_at=NOW - timedelta(hours=37),
    )
    cache = SQLAlchemyCatalogueRepository(engine)
    cache.publish(run.run_id)
    assert cache.search(CatalogueQuery(source_id="mock"), NOW).total == 0


def test_query_filters_are_exact_and_price_sort_requires_matching_units(
    engine: Engine,
    repository: SQLAlchemyOfferRepository,
    snapshots: FileSnapshotStore,
    candidate: dict[str, Any],
) -> None:
    offers = [
        variant(candidate, offer_key=f"variant-{i}", current_price=price)
        for i, price in enumerate(("19.9", "10.00", "15.12"))
    ]
    run = complete_batch(repository, snapshots, offers)
    cache = SQLAlchemyCatalogueRepository(engine)
    cache.publish(run.run_id)
    page = cache.search(
        CatalogueQuery(
            source_id="mock", unit="l", sort="unit_price", limit=1, offset=1, max_unit_price="16"
        ),
        NOW,
    )
    assert page.total == 2 and page.items[0].offer.current_price == Decimal("15.12")
    assert cache.search(CatalogueQuery(source_id="mock", search="%"), NOW).total == 0
    assert cache.search(CatalogueQuery(source_id="mock", retailers=("unknown",)), NOW).total == 0
    assert cache.search(CatalogueQuery(source_id="mock", scope="wrong"), NOW).total == 0
    with pytest.raises(ValidationError, match="requires a unit"):
        CatalogueQuery(source_id="mock", sort="unit_price")
    with pytest.raises(ValidationError):
        CatalogueQuery(source_id="mock", limit=101)


def test_failed_publication_rolls_back_derived_rows(
    engine: Engine,
    repository: SQLAlchemyOfferRepository,
    snapshots: FileSnapshotStore,
    candidate: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = complete_batch(repository, snapshots, [Offer.model_validate(candidate)])
    cache = SQLAlchemyCatalogueRepository(engine)
    cache.publish(first.run_id)
    second = complete_batch(
        repository,
        snapshots,
        [variant(candidate, current_price="18.9")],
        NOW + timedelta(minutes=1),
    )

    def fail() -> None:
        raise RuntimeError("publication interrupted")

    monkeypatch.setattr("grocery_agent.catalogue.repository.utc_now", fail)
    with pytest.raises(RuntimeError, match="interrupted"):
        cache.publish(second.run_id)
    assert cache.state("mock", NOW + timedelta(minutes=1)).run_id == first.run_id
    with repository.sessions() as session:
        assert list(session.scalars(select(CatalogueEntryRow.run_id))) == [first.run_id]


def test_reader_snapshot_survives_concurrent_publication(
    engine: Engine,
    repository: SQLAlchemyOfferRepository,
    snapshots: FileSnapshotStore,
    candidate: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = complete_batch(repository, snapshots, [Offer.model_validate(candidate)])
    reader, writer = SQLAlchemyCatalogueRepository(engine), SQLAlchemyCatalogueRepository(engine)
    writer.publish(first.run_id)
    second = complete_batch(
        repository,
        snapshots,
        [variant(candidate, current_price="18.9")],
        NOW + timedelta(minutes=1),
    )
    state = reader._state

    def publish_during_read(*args: Any) -> Any:
        selected = state(*args)
        writer.publish(second.run_id)
        return selected

    monkeypatch.setattr(reader, "_state", publish_during_read)
    page = reader.search(CatalogueQuery(source_id="mock"), NOW + timedelta(minutes=1))
    assert page.state.run_id == first.run_id and page.total == 1
    assert page.items[0].offer.current_price == Decimal("19.9")
    assert writer.state("mock", NOW + timedelta(minutes=1)).run_id == second.run_id


def test_final_state_within_one_batch_is_published_once(
    engine: Engine,
    repository: SQLAlchemyOfferRepository,
    snapshots: FileSnapshotStore,
    candidate: dict[str, Any],
) -> None:
    run = complete_batch(
        repository,
        snapshots,
        [Offer.model_validate(candidate), variant(candidate, current_price="18.9")],
    )
    cache = SQLAlchemyCatalogueRepository(engine)
    cache.publish(run.run_id)
    page = cache.search(CatalogueQuery(source_id="mock"), NOW)
    assert page.total == 1 and page.items[0].offer.current_price == Decimal("18.9")
