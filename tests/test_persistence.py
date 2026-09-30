import hashlib
import json
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError

from grocery_agent.matching.base import ExactGTINResolver
from grocery_agent.models.observation import PriceObservation
from grocery_agent.models.offer import Offer
from grocery_agent.persistence.repository import SQLAlchemyOfferRepository, state_fingerprint
from grocery_agent.persistence.schema import (
    ObservationRow,
    OfferRow,
    ProductRow,
    ScrapeItemRow,
    SnapshotRow,
    StoreProductRow,
)
from grocery_agent.persistence.snapshots import FileSnapshotStore
from grocery_agent.pipeline.results import ScrapeResult
from grocery_agent.stores.base import SourceEvidence


def persist(
    repository: SQLAlchemyOfferRepository,
    snapshots: FileSnapshotStore,
    candidate: dict[str, Any],
    *,
    at: datetime | None = None,
) -> bool:
    run = ScrapeResult(source_id="mock")
    repository.start_run(run)
    offer = Offer.model_validate(candidate)
    evidence = SourceEvidence(
        json.dumps(candidate).encode(),
        str(offer.source_url),
        "application/json",
        at or datetime.now(UTC),
        "$",
    )
    snapshot = snapshots.save(evidence)
    repository.add_snapshot(run.run_id, snapshot)
    changed = repository.accept(
        run.run_id,
        PriceObservation(
            offer=offer,
            observed_at=evidence.fetched_at,
            snapshot_id=snapshot.id,
        ),
        ExactGTINResolver().resolve(offer.product),
    )
    run.fetched = run.accepted = 1
    run.changed = int(changed)
    run.status = "success"
    run.finished_at = datetime.now(UTC)
    repository.finish_run(run)
    return changed


def test_persist_exact_money_and_roundtrip(
    repository: SQLAlchemyOfferRepository, snapshots: FileSnapshotStore, candidate: dict[str, Any]
) -> None:
    assert persist(repository, snapshots, candidate)
    with repository.sessions() as session:
        row = session.scalar(select(ObservationRow))
        assert row is not None
        assert row.current_price_units == 199000
        assert row.regular_price_units == 249000
        assert Offer.model_validate(row.payload) == Offer.model_validate(candidate)
        assert row.last_seen_at.tzinfo is UTC
        assert session.scalar(select(func.count()).select_from(ProductRow)) == 1
        sku = session.scalar(select(StoreProductRow))
        assert sku is not None and sku.product_id is not None


def test_unchanged_state_retains_each_evidence(
    repository: SQLAlchemyOfferRepository, snapshots: FileSnapshotStore, candidate: dict[str, Any]
) -> None:
    first_time = datetime(2026, 9, 28, tzinfo=UTC)
    assert persist(repository, snapshots, candidate, at=first_time)
    assert not persist(repository, snapshots, candidate, at=first_time + timedelta(days=1))
    with repository.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ObservationRow)) == 1
        assert session.scalar(select(func.count()).select_from(ScrapeItemRow)) == 2
        assert session.scalar(select(func.count()).select_from(SnapshotRow)) == 2
        row = session.scalar(select(ObservationRow))
        assert row is not None and row.first_seen_at == first_time
        assert row.last_seen_at == first_time + timedelta(days=1)
    assert len(list(snapshots.root.rglob("*.bin"))) == 1


def test_price_returning_to_prior_state_is_new_history(
    repository: SQLAlchemyOfferRepository, snapshots: FileSnapshotStore, candidate: dict[str, Any]
) -> None:
    candidate.update(promotion=None, regular_price=None)
    for day, price in enumerate(["19.90", "18.90", "19.90"]):
        candidate["current_price"] = price
        assert persist(
            repository,
            snapshots,
            candidate,
            at=datetime(2026, 9, 28, tzinfo=UTC) + timedelta(days=day),
        )
    with repository.sessions() as session:
        rows = list(session.scalars(select(ObservationRow).order_by(ObservationRow.version)))
        assert [row.current_price_units for row in rows] == [199000, 189000, 199000]
        assert [row.version for row in rows] == [1, 2, 3]


def test_simultaneous_offer_variants_and_scopes(
    repository: SQLAlchemyOfferRepository, snapshots: FileSnapshotStore, candidate: dict[str, Any]
) -> None:
    assert persist(repository, snapshots, candidate)
    candidate.update(offer_key="loyalty", promotion={"kind": "loyalty", "requires_loyalty": True})
    assert persist(repository, snapshots, candidate)
    candidate.update(scope="prague")
    assert persist(repository, snapshots, candidate)
    with repository.sessions() as session:
        assert session.scalar(select(func.count()).select_from(OfferRow)) == 3
        assert session.scalar(select(func.count()).select_from(StoreProductRow)) == 1


def test_decimal_and_unit_spellings_deduplicate(candidate: dict[str, Any]) -> None:
    first = Offer.model_validate(candidate)
    candidate["current_price"] = "19.9000"
    candidate["product"]["quantity"] = {"amount": "1000", "unit": "ml"}
    assert state_fingerprint(first) == state_fingerprint(Offer.model_validate(candidate))
    candidate["source_url"] = "https://mock.example.invalid/new-path"
    assert state_fingerprint(first) == state_fingerprint(Offer.model_validate(candidate))
    candidate["availability"] = "unavailable"
    assert state_fingerprint(first) != state_fingerprint(Offer.model_validate(candidate))


@pytest.mark.parametrize(
    "field,value",
    [
        ("valid_until", "2026-10-05"),
        ("regular_price", "24.80"),
        ("promotion", {"kind": "loyalty", "requires_loyalty": True}),
    ],
)
def test_non_current_price_changes_count(candidate: dict[str, Any], field: str, value: Any) -> None:
    first = Offer.model_validate(candidate)
    candidate[field] = value
    assert state_fingerprint(first) != state_fingerprint(Offer.model_validate(candidate))


def test_older_data_rolls_back_product_changes(
    repository: SQLAlchemyOfferRepository, snapshots: FileSnapshotStore, candidate: dict[str, Any]
) -> None:
    at = datetime(2026, 9, 30, tzinfo=UTC)
    persist(repository, snapshots, candidate, at=at)
    candidate["product"]["name"] = "Older bad description"
    with pytest.raises(ValueError, match="out-of-order"):
        persist(repository, snapshots, candidate, at=at - timedelta(days=1))
    with repository.sessions() as session:
        sku = session.scalar(select(StoreProductRow))
        assert sku is not None and sku.details["name"] != "Older bad description"
        assert session.scalar(select(func.count()).select_from(ObservationRow)) == 1


def test_missing_gtin_stays_unmatched(
    repository: SQLAlchemyOfferRepository, snapshots: FileSnapshotStore, candidate: dict[str, Any]
) -> None:
    candidate["product"]["gtin"] = None
    persist(repository, snapshots, candidate)
    with repository.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ProductRow)) == 0
        sku = session.scalar(select(StoreProductRow))
        assert sku is not None and sku.product_id is None


def test_missing_gtin_does_not_erase_previous_match(
    repository: SQLAlchemyOfferRepository, snapshots: FileSnapshotStore, candidate: dict[str, Any]
) -> None:
    persist(repository, snapshots, candidate)
    candidate["product"]["gtin"] = None
    persist(repository, snapshots, candidate)
    with repository.sessions() as session:
        sku = session.scalar(select(StoreProductRow))
        assert sku is not None and sku.product_id is not None


def test_run_results_survive_reopening(
    engine: Engine,
    repository: SQLAlchemyOfferRepository,
    snapshots: FileSnapshotStore,
    candidate: dict[str, Any],
) -> None:
    persist(repository, snapshots, candidate)
    reopened = SQLAlchemyOfferRepository(engine)
    runs = reopened.recent_runs(1)
    assert len(runs) == 1 and runs[0]["accepted"] == 1 and runs[0]["status"] == "success"
    assert runs[0]["started_at"] and runs[0]["finished_at"]


def test_foreign_keys_enforced(repository: SQLAlchemyOfferRepository) -> None:
    with pytest.raises(IntegrityError), repository.sessions.begin() as session:
        session.add(OfferRow(id="x", store_product_id="nonexistent", offer_key="x", scope="x"))
        session.flush()


def test_snapshot_integrity_and_deduplication(snapshots: FileSnapshotStore) -> None:
    evidence = SourceEvidence(
        b"raw bytes", "https://example.invalid", "text/plain", datetime.now(UTC), "page 1"
    )
    first, second = snapshots.save(evidence), snapshots.save(evidence)
    assert first.id != second.id and first.sha256 == second.sha256
    content = (snapshots.root / first.relative_path).read_bytes()
    assert content == evidence.content and hashlib.sha256(content).hexdigest() == first.sha256
    assert not list(snapshots.root.rglob("tmp*"))


def test_non_utc_timestamps_roundtrip(
    repository: SQLAlchemyOfferRepository, snapshots: FileSnapshotStore, candidate: dict[str, Any]
) -> None:
    at = datetime(2026, 9, 30, 12, tzinfo=timezone(timedelta(hours=2)))
    persist(repository, snapshots, candidate, at=at)
    with repository.sessions() as session:
        row = session.scalar(select(ObservationRow))
        assert row is not None and row.first_seen_at == at.astimezone(UTC)


def test_fractional_price_is_stored_exactly(
    repository: SQLAlchemyOfferRepository, snapshots: FileSnapshotStore, candidate: dict[str, Any]
) -> None:
    candidate.update(current_price="0.1234", promotion=None, regular_price=None)
    persist(repository, snapshots, candidate)
    with repository.sessions() as session:
        row = session.scalar(select(ObservationRow))
        assert row is not None and row.current_price_units == 1234
        assert Offer.model_validate(row.payload).current_price == Decimal("0.1234")
