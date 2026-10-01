from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError, OperationalError

from grocery_agent.application.parameters import (
    MealOverrides,
    MealParameters,
    PolicyOverrides,
    resolve_parameters,
)
from grocery_agent.meals.catalog import MealCatalog
from grocery_agent.models.offer import Offer
from grocery_agent.persistence.control.repository import SQLAlchemyProfileRepository
from grocery_agent.persistence.control.schema import ControlBase, UserRow
from grocery_agent.persistence.database import create_database_engine, open_database
from grocery_agent.persistence.migrations import (
    require_offer_schema,
    schema_version,
    upgrade_database,
)
from grocery_agent.persistence.migrations.baselines import offer_baseline
from grocery_agent.persistence.repository import SQLAlchemyOfferRepository
from grocery_agent.persistence.schema import Base, ObservationRow, SnapshotRow
from grocery_agent.persistence.snapshots import FileSnapshotStore
from tests.application_support import NOW, complete_batch

ROOT = Path(__file__).resolve().parents[1]


def test_independent_schemas_and_revisions(tmp_path: Path) -> None:
    offers = open_database(f"sqlite:///{tmp_path / 'offers.db'}")
    control = create_database_engine(f"sqlite:///{tmp_path / 'control.db'}")
    try:
        assert upgrade_database(control, "control") == "control_0001"
        assert schema_version(offers, "offers") == "offers_0002"
        assert set(inspect(control).get_table_names()) == {
            "users",
            "user_profiles",
            "control_schema_version",
        }
        assert "users" not in inspect(offers).get_table_names()
        assert "price_observations" not in inspect(control).get_table_names()
        with pytest.raises(ValueError, match="separate"):
            upgrade_database(offers, "control")
        with pytest.raises(ValueError, match="separate"):
            upgrade_database(control, "offers")
    finally:
        offers.dispose()
        control.dispose()


def test_legacy_adoption_preserves_prices_and_snapshot_references(
    tmp_path: Path,
    candidate: dict[str, Any],
) -> None:
    path = tmp_path / "legacy.db"
    legacy = create_database_engine(f"sqlite:///{path}")
    metadata = offer_baseline()
    metadata.create_all(legacy)
    repository = SQLAlchemyOfferRepository(legacy)
    run = complete_batch(
        repository, FileSnapshotStore(tmp_path / "snapshots"), [Offer.model_validate(candidate)]
    )
    with legacy.connect() as connection:
        original = {
            name: connection.execute(select(table)).mappings().all()
            for name, table in metadata.tables.items()
        }
    assert schema_version(legacy, "offers") is None
    require_offer_schema(legacy)  # Readability does not stamp or bootstrap the database.
    assert "offers_schema_version" not in inspect(legacy).get_table_names()
    assert upgrade_database(legacy, "offers") == "offers_0002"
    assert upgrade_database(legacy, "offers") == "offers_0002"
    with legacy.connect() as connection:
        after = {
            name: connection.execute(select(table)).mappings().all()
            for name, table in metadata.tables.items()
        }
    assert original == after
    with repository.sessions() as session:
        assert session.scalar(select(ObservationRow)).current_price_units == 199000
        assert session.scalar(select(SnapshotRow)).run_id == run.run_id
    legacy.dispose()


@pytest.mark.parametrize("malformation", ["partial", "extra", "wrong_type", "index"])
def test_malformed_legacy_refused_without_stamping(tmp_path: Path, malformation: str) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'broken.db'}")
    metadata = offer_baseline()
    if malformation == "partial":
        metadata.create_all(engine, tables=[metadata.tables["products"]])
    else:
        metadata.create_all(engine)
        with engine.begin() as connection:
            if malformation == "extra":
                connection.exec_driver_sql("CREATE TABLE unrelated (id INTEGER)")
            elif malformation == "wrong_type":
                connection.exec_driver_sql("ALTER TABLE products ADD COLUMN unexpected TEXT")
            else:
                connection.exec_driver_sql("DROP INDEX ix_scrape_runs_started_at")
    before = set(inspect(engine).get_table_names())
    with pytest.raises(ValueError, match="schema"):
        upgrade_database(engine, "offers")
    assert set(inspect(engine).get_table_names()) == before
    assert "offers_schema_version" not in before
    engine.dispose()


def test_read_only_engine_does_not_create_or_upgrade_storage(tmp_path: Path) -> None:
    absent = tmp_path / "absent.db"
    engine = create_database_engine(f"sqlite:///{absent}", read_only=True)
    with pytest.raises(OperationalError), engine.connect():
        pass
    assert not absent.exists()
    engine.dispose()
    path = tmp_path / "existing.db"
    writer = open_database(f"sqlite:///{path}")
    tables = inspect(writer).get_table_names()
    reader = create_database_engine(f"sqlite:///{path}", read_only=True)
    require_offer_schema(reader)
    with pytest.raises(OperationalError), reader.begin() as connection:
        connection.execute(text("CREATE TABLE must_not_exist (id INTEGER)"))
    assert inspect(writer).get_table_names() == tables
    reader.dispose()
    writer.dispose()


def test_migration_heads_match_orm_metadata(tmp_path: Path) -> None:
    for target, metadata in (("offers", Base.metadata), ("control", ControlBase.metadata)):
        engine = create_database_engine(f"sqlite:///{tmp_path / (target + '.db')}")
        upgrade_database(engine, target)
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={
                    "version_table": f"{target}_schema_version",
                    "compare_type": True,
                },
            )
            assert compare_metadata(context, metadata) == []
        engine.dispose()


def test_profile_ownership_and_optimistic_revision(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'users.db'}")
    upgrade_database(engine, "control")
    first, second = uuid4(), uuid4()
    with engine.begin() as connection:
        connection.execute(
            UserRow.__table__.insert(),
            [
                {"id": str(first), "username": "alice", "created_at": NOW},
                {"id": str(second), "username": "bob", "created_at": NOW},
            ],
        )
    catalog = MealCatalog.load(ROOT / "config/meals.toml")
    defaults = MealParameters.from_catalog(catalog)
    repository = SQLAlchemyProfileRepository(engine)
    profile = repository.create(first, "Dinner", defaults)
    assert repository.get(first, profile.id) == profile
    assert repository.get(second, profile.id) is None
    changed = resolve_parameters(catalog, MealOverrides(policy=PolicyOverrides(servings=2)))
    with pytest.raises(ValueError, match="unavailable"):
        repository.update(second, profile.id, changed, 1)
    updated = repository.update(first, profile.id, changed, 1)
    assert updated.revision == 2 and updated.parameters.policy.servings == 2
    with pytest.raises(ValueError, match="changed"):
        repository.update(first, profile.id, defaults, 1)
    assert repository.get(first, profile.id) == updated
    with pytest.raises(IntegrityError):
        repository.create(uuid4(), "No owner", defaults)
    engine.dispose()
