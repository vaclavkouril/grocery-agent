"""Offline acceptance contracts for profile publication and legacy migration."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import Engine, MetaData, Table, inspect, select

from grocery_agent.catalogue.config import CatalogueSettings
from grocery_agent.catalogue.models import CatalogueQuery
from grocery_agent.catalogue.profiles import AcquisitionProfile, Coverage
from grocery_agent.catalogue.repository import SQLAlchemyCatalogueRepository
from grocery_agent.catalogue.schema import CatalogueEntryRow, CatalogueHeadRow
from grocery_agent.models.offer import Offer
from grocery_agent.persistence.database import create_database_engine
from grocery_agent.persistence.migrations import migration_config, upgrade_database
from grocery_agent.persistence.repository import SQLAlchemyOfferRepository
from grocery_agent.persistence.schema import ObservationRow, ScrapeRunRow, SnapshotRow
from grocery_agent.persistence.snapshots import FileSnapshotStore
from grocery_agent.pipeline.results import ScrapeResult
from tests.application_support import NOW, complete_batch, failed_batch


def profile(name: str = "full", **changes: Any) -> AcquisitionProfile:
    return AcquisitionProfile.model_validate({"source_id": "mock", "name": name, **changes})


def coverage(p: AcquisitionProfile, observed: int = 1, **changes: Any) -> Coverage:
    return Coverage.model_validate(
        {"profile_fingerprint": p.fingerprint, "observed": observed, "complete": True, **changes}
    )


class ProfiledFixtureRepository(SQLAlchemyOfferRepository):
    """Give the shared batch helper the identity a pipeline supplies before fetching."""

    def __init__(self, engine: Engine, p: AcquisitionProfile) -> None:
        super().__init__(engine)
        self.profile = p

    def start_run(self, result: ScrapeResult) -> None:
        result.profile_fingerprint = self.profile.fingerprint
        result.acquisition_profile = self.profile.model_dump(mode="json")
        super().start_run(result)


class PreProfileFixtureRepository(SQLAlchemyOfferRepository):
    """Seed the old schema using its original run insert, before offers_0003 exists."""

    def start_run(self, result: ScrapeResult) -> None:
        with self.sessions.begin() as session:
            session.add(
                ScrapeRunRow(
                    id=result.run_id,
                    source_id=result.source_id,
                    started_at=result.started_at,
                    status=result.status,
                )
            )


@dataclass
class CatalogueFixture:
    engine: Engine
    repository: SQLAlchemyOfferRepository
    snapshots: FileSnapshotStore
    candidate: dict[str, Any]
    cache: SQLAlchemyCatalogueRepository

    def run(
        self,
        key: str,
        at: datetime = NOW,
        source: str = "mock",
        scopes: tuple[str, ...] = ("national",),
        p: AcquisitionProfile | None = None,
    ) -> ScrapeResult:
        offers = [
            Offer.model_validate(
                {
                    **self.candidate,
                    "offer_key": f"{key}-{i}",
                    "scope": scope,
                    "regular_price": None,
                    "promotion": None,
                    "valid_from": None,
                    "valid_until": None,
                }
            )
            for i, scope in enumerate(scopes)
        ]
        repository = ProfiledFixtureRepository(self.engine, p) if p else self.repository
        return complete_batch(repository, self.snapshots, offers, at, source=source)

    def publish(
        self,
        p: AcquisitionProfile,
        key: str,
        at: datetime = NOW,
        scopes: tuple[str, ...] = ("national",),
        **metadata: Any,
    ) -> ScrapeResult:
        run = self.run(key, at, p.source_id, scopes, p=p)
        self.cache.record_profile(run.run_id, p, coverage(p, run.accepted, **metadata))
        self.cache.publish(run.run_id)
        return run

    def page(self, p: AcquisitionProfile, at: datetime = NOW) -> Any:
        return self.cache.search(
            CatalogueQuery(source_id=p.source_id, profile_fingerprint=p.fingerprint), at
        )


@pytest.fixture
def catalogue(
    engine: Engine,
    repository: SQLAlchemyOfferRepository,
    snapshots: FileSnapshotStore,
    candidate: dict[str, Any],
) -> CatalogueFixture:
    return CatalogueFixture(
        engine, repository, snapshots, candidate, SQLAlchemyCatalogueRepository(engine)
    )


def table(engine: Engine, name: str) -> Table:
    return Table(name, MetaData(), autoload_with=engine)


def test_category_profile_does_not_replace_full_or_legacy(catalogue: CatalogueFixture) -> None:
    legacy = catalogue.run("legacy")
    catalogue.cache.publish(legacy.run_id)
    full, narrow = profile(), profile("produce", categories=("fruit",))
    full_run = catalogue.publish(full, "full", NOW + timedelta(minutes=1))
    narrow_run = catalogue.publish(narrow, "fruit", NOW + timedelta(minutes=2))
    now = NOW + timedelta(minutes=3)
    assert catalogue.page(full, now).state.run_id == full_run.run_id
    assert catalogue.page(narrow, now).state.run_id == narrow_run.run_id
    assert catalogue.cache.state("mock", now).run_id == legacy.run_id
    assert catalogue.cache.state("mock", now, "legacy").run_id == legacy.run_id
    assert catalogue.page(full, now).total == catalogue.page(narrow, now).total == 1
    assert catalogue.cache.search(CatalogueQuery(source_id="mock"), now).total == 1


def test_explicit_profile_does_not_create_a_legacy_head(catalogue: CatalogueFixture) -> None:
    catalogue.publish(profile(), "full")
    with pytest.raises(ValueError):
        catalogue.cache.state("mock", NOW)
    with catalogue.repository.sessions() as session:
        assert session.get(CatalogueHeadRow, "mock") is None


def test_legacy_publication_updates_both_legacy_heads(catalogue: CatalogueFixture) -> None:
    first = catalogue.run("old")
    catalogue.cache.publish(first.run_id)
    p = profile()
    profiled = catalogue.publish(p, "full", NOW + timedelta(minutes=1))
    second = catalogue.run("new", NOW + timedelta(minutes=2))
    catalogue.cache.publish(second.run_id)
    now = NOW + timedelta(minutes=3)
    assert catalogue.cache.state("mock", now).run_id == second.run_id
    assert catalogue.cache.state("mock", now, "legacy").run_id == second.run_id
    assert catalogue.page(p, now).state.run_id == profiled.run_id
    assert catalogue.page(p, now).total == 1
    state = catalogue.cache.state("mock", now)
    assert state.coverage_complete is False
    assert any("coverage" in warning.lower() for warning in state.warnings)


@pytest.mark.parametrize("status", ["failed", "partial", "empty", "cancelled"])
def test_failed_refresh_uses_fresh_previous_same_profile(
    catalogue: CatalogueFixture,
    status: str,
) -> None:
    p, other = profile(), profile("fruit", categories=("fruit",))
    previous = catalogue.publish(p, "full")
    other_run = catalogue.publish(other, "fruit", NOW + timedelta(minutes=1))
    failure = failed_batch(
        ProfiledFixtureRepository(catalogue.engine, p), NOW + timedelta(minutes=2), status
    )
    catalogue.cache.record_profile(failure.run_id, p, coverage(p, 0, complete=False))
    with pytest.raises(ValueError):
        catalogue.cache.publish(failure.run_id)
    now = NOW + timedelta(hours=1)
    state = catalogue.page(p, now).state
    assert state.run_id == previous.run_id and state.degraded
    assert state.latest_run_id == failure.run_id and state.latest_run_status == status
    assert any(status in warning and previous.run_id in warning for warning in state.warnings)
    unaffected = catalogue.page(other, now).state
    assert unaffected.run_id == other_run.run_id and not unaffected.degraded
    assert unaffected.latest_run_id == other_run.run_id
    strict = SQLAlchemyCatalogueRepository(
        catalogue.engine, CatalogueSettings(allow_cached_on_failure=False)
    )
    with pytest.raises(ValueError):
        strict.state("mock", now, p.fingerprint)
    assert strict.state("mock", now, other.fingerprint).run_id == other_run.run_id


def test_profile_expiry_is_not_extended_by_other_profile(catalogue: CatalogueFixture) -> None:
    p, other = profile(), profile("fruit", categories=("fruit",))
    catalogue.publish(p, "full")
    newer = catalogue.publish(other, "fruit", NOW + timedelta(hours=35))
    assert catalogue.page(p, NOW + timedelta(hours=36)).total == 1
    stale = NOW + timedelta(hours=36, microseconds=1)
    with pytest.raises(ValueError, match="stale"):
        catalogue.page(p, stale)
    assert catalogue.page(other, stale).state.run_id == newer.run_id
    listed = catalogue.cache.collections("mock")
    assert {row["profile_fingerprint"] for row in listed} == {p.fingerprint, other.fingerprint}


@pytest.mark.parametrize("complete,expected", [(False, None), (True, 2)])
def test_unpublishable_coverage_preserves_previous_head(
    catalogue: CatalogueFixture,
    complete: bool,
    expected: int | None,
) -> None:
    p = profile()
    previous = catalogue.publish(p, "previous")
    run = catalogue.run("incomplete", NOW + timedelta(minutes=1), p=p)
    catalogue.cache.record_profile(run.run_id, p, coverage(p, complete=complete, expected=expected))
    with pytest.raises(ValueError):
        catalogue.cache.publish(run.run_id)
    assert catalogue.page(p, NOW + timedelta(minutes=2)).state.run_id == previous.run_id
    with catalogue.repository.sessions() as session:
        assert run.run_id not in set(session.scalars(select(CatalogueEntryRow.run_id)))


def test_partial_profile_cannot_publish_even_with_complete_coverage(
    catalogue: CatalogueFixture,
) -> None:
    p = profile("partial", coverage="partial")
    run = catalogue.run("partial", p=p)
    catalogue.cache.record_profile(run.run_id, p, coverage(p))
    with pytest.raises(ValueError):
        catalogue.cache.publish(run.run_id)


def test_zero_item_success_cannot_publish(catalogue: CatalogueFixture) -> None:
    p = profile()
    previous = catalogue.publish(p, "previous")
    empty = complete_batch(
        ProfiledFixtureRepository(catalogue.engine, p),
        catalogue.snapshots,
        [],
        NOW + timedelta(minutes=1),
    )
    catalogue.cache.record_profile(empty.run_id, p, coverage(p, 0))
    with pytest.raises(ValueError):
        catalogue.cache.publish(empty.run_id)
    assert catalogue.page(p, NOW + timedelta(minutes=2)).state.run_id == previous.run_id


@pytest.mark.parametrize("mismatch", ["source", "fingerprint", "observed-high", "missing-run"])
def test_record_profile_validates_run_identity_and_accepted_count(
    catalogue: CatalogueFixture,
    mismatch: str,
) -> None:
    run = catalogue.run("one", p=profile())
    p = profile(source_id="other") if mismatch == "source" else profile()
    metadata = coverage(p)
    if mismatch == "fingerprint":
        metadata = coverage(p, profile_fingerprint="wrong")
    elif mismatch == "observed-high":
        metadata = coverage(p, 2)
    with pytest.raises(ValueError):
        catalogue.cache.record_profile(
            str(uuid4()) if mismatch == "missing-run" else run.run_id, p, metadata
        )


def test_record_profile_allows_observed_below_accepted(catalogue: CatalogueFixture) -> None:
    p = profile()
    offer = Offer.model_validate(catalogue.candidate)
    run = complete_batch(
        ProfiledFixtureRepository(catalogue.engine, p), catalogue.snapshots, [offer, offer]
    )
    assert run.accepted == 2
    catalogue.cache.record_profile(run.run_id, p, coverage(p, 1))
    catalogue.cache.publish(run.run_id)
    assert catalogue.page(p).total == 1


def test_search_requires_source_and_exact_fingerprint(catalogue: CatalogueFixture) -> None:
    p = profile()
    catalogue.publish(p, "full")
    for source, fingerprint in [("other", p.fingerprint), ("mock", profile("missing").fingerprint)]:
        with pytest.raises(ValueError):
            catalogue.cache.search(
                CatalogueQuery(source_id=source, profile_fingerprint=fingerprint), NOW
            )
    assert catalogue.page(p).state.profile_fingerprint == p.fingerprint


def test_each_profile_head_retains_its_indexed_entries(catalogue: CatalogueFixture) -> None:
    legacy = catalogue.run("legacy")
    catalogue.cache.publish(legacy.run_id)
    profiles = [
        profile(),
        profile("fruit", categories=("fruit",)),
        profile("milk", search=("milk",)),
    ]
    runs = [
        catalogue.publish(p, p.name, NOW + timedelta(minutes=i + 1)) for i, p in enumerate(profiles)
    ]
    replacement = catalogue.publish(profiles[1], "new-fruit", NOW + timedelta(minutes=5))
    runs[1] = replacement
    catalogue.cache.publish(replacement.run_id)
    now = NOW + timedelta(minutes=6)
    for p, run in zip(profiles, runs, strict=True):
        page = catalogue.page(p, now)
        assert page.state.run_id == run.run_id and page.total == 1
    with catalogue.repository.sessions() as session:
        indexed = list(session.scalars(select(CatalogueEntryRow.run_id)))
        assert set(indexed) == {legacy.run_id, *(run.run_id for run in runs)}
        assert len(indexed) == 4
        assert len(list(session.scalars(select(ObservationRow.id)))) == 5
    heads = table(catalogue.engine, "catalogue_profile_heads")
    with catalogue.engine.connect() as connection:
        assert set(connection.execute(select(heads.c.run_id)).scalars()) == set(indexed)


def test_overlapping_publish_calls_do_not_let_older_run_replace_newer(
    catalogue: CatalogueFixture,
) -> None:
    p = profile()
    older = catalogue.run("older", p=p)
    newer = catalogue.run("newer", NOW + timedelta(minutes=1), p=p)
    for run in (older, newer):
        catalogue.cache.record_profile(run.run_id, p, coverage(p))
    entered, release = Event(), Event()

    def late_older_publish() -> None:
        writer = SQLAlchemyCatalogueRepository(catalogue.engine)
        entered.set()
        assert release.wait(5), "newer publisher did not finish"
        writer.publish(older.run_id)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(late_older_publish)
        try:
            assert entered.wait(5)
            SQLAlchemyCatalogueRepository(catalogue.engine).publish(newer.run_id)
        finally:
            release.set()
        with pytest.raises(ValueError, match="older"):
            pending.result(timeout=5)
    assert catalogue.page(p, NOW + timedelta(minutes=2)).state.run_id == newer.run_id


@pytest.mark.parametrize("known", [False, True])
def test_actual_scope_metadata_is_recorded_without_guessing(
    catalogue: CatalogueFixture,
    known: bool,
) -> None:
    p = profile()
    scopes = ("actual-location-a", "actual-location-b")
    metadata = {"actual_scopes": scopes, "actual_scope": None} if known else {}
    run = catalogue.publish(p, "locations", scopes=scopes, **metadata)
    state = catalogue.page(p).state
    assert state.actual_scope is None
    assert state.actual_scopes == scopes
    assert state.coverage_complete is True
    records = table(catalogue.engine, "catalogue_run_profiles")
    with catalogue.engine.connect() as connection:
        row = (
            connection.execute(select(records).where(records.c.run_id == run.run_id))
            .mappings()
            .one()
        )
    assert row["profile"] == p.model_dump(mode="json")
    assert row["coverage"]["actual_scope"] is None
    assert row["coverage"]["actual_scopes"] == list(scopes)


def test_single_actual_scope_round_trips(catalogue: CatalogueFixture) -> None:
    p = profile(scope="observed")
    catalogue.publish(
        p, "actual", scopes=("observed",), actual_scope="observed", actual_scopes=("observed",)
    )
    state = catalogue.page(p).state
    assert state.actual_scope == "observed" and state.actual_scopes == ("observed",)


def test_running_profile_identity_does_not_degrade_legacy_or_other_profile(
    catalogue: CatalogueFixture,
) -> None:
    legacy = catalogue.run("legacy")
    catalogue.cache.publish(legacy.run_id)
    p, other = profile(), profile("fruit", categories=("fruit",))
    previous = catalogue.publish(p, "full", NOW + timedelta(minutes=1))
    other_run = catalogue.publish(other, "fruit", NOW + timedelta(minutes=2))
    running = ScrapeResult(source_id="mock", started_at=NOW + timedelta(minutes=3))
    ProfiledFixtureRepository(catalogue.engine, p).start_run(running)
    now = NOW + timedelta(minutes=4)
    state = catalogue.page(p, now).state
    assert state.run_id == previous.run_id and state.degraded
    assert state.latest_run_id == running.run_id and state.latest_run_status == "running"
    assert catalogue.page(other, now).state.latest_run_id == other_run.run_id
    assert not catalogue.page(other, now).state.degraded
    legacy_state = catalogue.cache.state("mock", now)
    assert legacy_state.latest_run_id == legacy.run_id and not legacy_state.degraded
    records = table(catalogue.engine, "catalogue_run_profiles")
    with catalogue.engine.connect() as connection:
        row = (
            connection.execute(select(records).where(records.c.run_id == running.run_id))
            .mappings()
            .one()
        )
    assert row["profile_fingerprint"] == p.fingerprint
    assert row["coverage"]["complete"] is False
    assert row["coverage"]["actual_scopes"] == []
    assert row["coverage"].get("actual_scope") is None


@pytest.mark.parametrize(
    "changes",
    [
        {"actual_scope": "invented"},
        {"actual_scopes": ("invented",)},
    ],
)
def test_record_profile_rejects_invented_actual_scopes(
    catalogue: CatalogueFixture,
    changes: dict[str, Any],
) -> None:
    p = profile()
    run = catalogue.run("actual", scopes=("observed",), p=p)
    with pytest.raises(ValueError):
        catalogue.cache.record_profile(run.run_id, p, coverage(p, **changes))


def test_published_profile_metadata_is_immutable(catalogue: CatalogueFixture) -> None:
    p = profile()
    run = catalogue.publish(p, "full")
    catalogue.cache.record_profile(run.run_id, p, coverage(p))
    with pytest.raises(ValueError):
        catalogue.cache.record_profile(run.run_id, p, coverage(p, complete=False))
    with pytest.raises(ValueError):
        other = profile("other")
        catalogue.cache.record_profile(run.run_id, other, coverage(other))
    assert catalogue.page(p).state.coverage_complete


def test_collections_list_all_sources_and_stale_metadata(catalogue: CatalogueFixture) -> None:
    p, other = profile(), profile("other-full", source_id="other")
    first = catalogue.publish(p, "mock")
    second = catalogue.publish(other, "other")
    with pytest.raises(ValueError, match="stale"):
        catalogue.page(p, NOW + timedelta(hours=37))
    rows = catalogue.cache.collections()
    assert {(row["source_id"], row["profile_fingerprint"], row["run_id"]) for row in rows} == {
        ("mock", p.fingerprint, first.run_id),
        ("other", other.fingerprint, second.run_id),
    }
    assert [row["run_id"] for row in catalogue.cache.collections("mock")] == [first.run_id]
    assert catalogue.cache.collections("absent") == []


def test_offers_0003_preserves_history_and_backfills_all_legacy_runs(
    tmp_path: Path,
    snapshots: FileSnapshotStore,
    candidate: dict[str, Any],
) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'old-offers.db'}")
    try:
        config = migration_config("offers")
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "offers_0002")
        repository = PreProfileFixtureRepository(engine)
        fixture = CatalogueFixture(
            engine, repository, snapshots, candidate, SQLAlchemyCatalogueRepository(engine)
        )
        historic = fixture.run("historic", NOW - timedelta(minutes=1))
        published = fixture.run("published", scopes=("observed-a", "observed-b"))
        failure = failed_batch(repository, NOW + timedelta(minutes=1))
        other = fixture.run("other", source="other")
        # Seed the old indexed cache directly: do not invoke the new publisher on old DDL.
        with repository.sessions.begin() as session:
            observations = list(
                session.scalars(
                    select(ObservationRow).where(
                        ObservationRow.payload["offer_key"].as_string().like("published-%")
                    )
                )
            )
            for observation in observations:
                offer = Offer.model_validate(observation.payload)
                session.add(
                    CatalogueEntryRow(
                        id=str(uuid4()),
                        run_id=published.run_id,
                        observation_id=observation.id,
                        scope=offer.scope,
                        retailer_id=offer.product.store_id,
                        normalized_name=offer.product.normalized_name,
                        category=offer.product.category,
                        unit=offer.unit_price.unit.value,
                        currency=offer.currency.value,
                        unit_price_units=int(offer.unit_price.amount * 10000),
                        requires_loyalty=False,
                        price_qualifier="exact",
                        availability=offer.availability.value,
                        observed_at=NOW,
                        valid_from=None,
                        valid_until=None,
                    )
                )
            session.add(
                CatalogueHeadRow(source_id="mock", run_id=published.run_id, published_at=NOW)
            )
        preserved = [
            "scrape_runs",
            "price_observations",
            "source_snapshots",
            "scrape_items",
            "offers",
            "catalogue_entries",
            "catalogue_heads",
        ]
        with engine.connect() as connection:
            before = {
                name: list(connection.execute(select(table(engine, name))).mappings())
                for name in preserved
            }
        assert upgrade_database(engine, "offers") == "offers_0003"
        assert upgrade_database(engine, "offers") == "offers_0003"
        with engine.connect() as connection:
            for name in preserved:
                assert (
                    list(connection.execute(select(table(engine, name))).mappings()) == before[name]
                )
            records = table(engine, "catalogue_run_profiles")
            rows = list(connection.execute(select(records)).mappings())
            assert {row["run_id"] for row in rows} == {
                historic.run_id,
                published.run_id,
                failure.run_id,
                other.run_id,
            }
            for row in rows:
                assert row["profile_fingerprint"] == "legacy"
                assert row["coverage"]["complete"] is False
                assert row["coverage"].get("actual_scope") is None
                assert row["coverage"]["actual_scopes"] == []
                assert row["profile"] is None
                assert row["source_id"] == ("other" if row["run_id"] == other.run_id else "mock")
            heads = table(engine, "catalogue_profile_heads")
            copied = connection.execute(select(heads)).mappings().one()
            assert copied["source_id"] == "mock" and copied["profile_fingerprint"] == "legacy"
            assert copied["run_id"] == published.run_id
            assert copied["published_at"].replace(tzinfo=NOW.tzinfo) == NOW
        inspector = inspect(engine)
        assert inspector.get_pk_constraint("catalogue_run_profiles")["constrained_columns"] == [
            "run_id"
        ]
        assert any(
            index["column_names"] == ["source_id"]
            for index in inspector.get_indexes("catalogue_run_profiles")
        )
        assert set(
            inspector.get_pk_constraint("catalogue_profile_heads")["constrained_columns"]
        ) == {"source_id", "profile_fingerprint"}
        assert any(
            fk["constrained_columns"] == ["run_id"] and fk["referred_table"] == "scrape_runs"
            for fk in inspector.get_foreign_keys("catalogue_run_profiles")
        )
        cache = SQLAlchemyCatalogueRepository(engine)
        page = cache.search(CatalogueQuery(source_id="mock"), NOW + timedelta(minutes=2))
        assert page.total == 2 and page.state.run_id == published.run_id
        assert page.state.coverage_complete is False
        assert page.state.actual_scope is None
        assert any("coverage" in warning.lower() for warning in page.state.warnings)
        assert cache.state("mock", NOW + timedelta(minutes=2), "legacy").run_id == published.run_id
        assert len(before[ObservationRow.__tablename__]) == 4
        assert len(before[SnapshotRow.__tablename__]) == 4
        assert len(before[ScrapeRunRow.__tablename__]) == 4
    finally:
        engine.dispose()
