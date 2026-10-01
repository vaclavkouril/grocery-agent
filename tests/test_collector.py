from datetime import UTC, datetime, time
from pathlib import Path

import pytest
from pydantic import ValidationError

from grocery_agent.catalogue.repository import SQLAlchemyCatalogueRepository
from grocery_agent.collector.config import CollectorSettings
from grocery_agent.collector.schedule import next_collection
from grocery_agent.collector.service import CollectorService
from grocery_agent.config import Settings
from grocery_agent.persistence.database import create_database_engine
from grocery_agent.stores.mock.adapter import MockStore
from grocery_agent.stores.registry import StoreRegistry


@pytest.mark.parametrize(
    "now,expected",
    [
        (datetime(2026, 10, 1, 8, tzinfo=UTC), datetime(2026, 10, 2, 0, tzinfo=UTC)),
        (datetime(2026, 10, 1, 23, tzinfo=UTC), datetime(2026, 10, 2, 0, tzinfo=UTC)),
        (datetime(2026, 3, 28, 12, tzinfo=UTC), datetime(2026, 3, 29, 1, tzinfo=UTC)),
        (datetime(2026, 10, 24, 12, tzinfo=UTC), datetime(2026, 10, 25, 0, tzinfo=UTC)),
        # Do not run again at the second autumn occurrence of 02:00.
        (datetime(2026, 10, 25, 0, 30, tzinfo=UTC), datetime(2026, 10, 26, 1, tzinfo=UTC)),
    ],
)
def test_daily_prague_schedule_handles_daylight_saving(now: datetime, expected: datetime) -> None:
    assert next_collection(now, time(2), "Europe/Prague") == expected


def test_schedule_requires_timezone_and_skips_an_already_run_day() -> None:
    now = datetime(2026, 10, 1, 8, tzinfo=UTC)
    assert next_collection(now, time(20), "Europe/Prague", now.date()).date().day == 2
    with pytest.raises(ValueError, match="aware"):
        next_collection(now.replace(tzinfo=None), time(2), "Europe/Prague")


def test_collector_toml_environment_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "collector.toml"
    path.write_text('sources = ["mock"]\ndaily_at = "01:00"\nrun_on_startup = false\n')
    (tmp_path / ".env").write_text(
        "GROCERY_DATABASE_URL=sqlite:///unrelated.db\nGROCERY_COLLECTOR_DAILY_AT=03:00\n"
    )
    assert CollectorSettings.load(path).daily_at == "03:00"
    monkeypatch.setenv("GROCERY_COLLECTOR_DAILY_AT", "02:00")
    settings = CollectorSettings.load(path)
    assert settings.sources == ("mock",) and settings.daily_at == "02:00"
    assert not settings.run_on_startup
    path.write_text("unknown = true\n")
    with pytest.raises(ValueError, match="unknown"):
        CollectorSettings.load(path)


@pytest.mark.parametrize("time_text", ["2:00", "24:00", "02:60", "tomorrow"])
def test_invalid_collection_time(time_text: str) -> None:
    with pytest.raises(ValidationError):
        CollectorSettings(daily_at=time_text, _env_file=None)


@pytest.mark.asyncio
async def test_independent_one_off_collector_needs_no_users(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'offers.db'}",
        snapshot_dir=tmp_path / "snapshots",
        report_dir=tmp_path / "reports",
        lock_path=tmp_path / "collector.lock",
        _env_file=None,
    )
    registry = StoreRegistry()
    registry.register("mock", MockStore)
    service = CollectorService(
        settings, CollectorSettings(sources=("mock",), _env_file=None), registry
    )
    (first,) = await service.collect_once()
    (second,) = await service.collect_once()
    assert first.accepted == 5 and first.changed == 5 and second.changed == 0
    engine = create_database_engine(settings.database_url, read_only=True)
    assert (
        SQLAlchemyCatalogueRepository(engine).state("mock", second.finished_at).run_id
        == second.run_id
    )
    engine.dispose()
    assert not settings.report_dir.exists() and not (tmp_path / "control.db").exists()


def test_collector_rejects_unknown_sources_before_creating_storage(tmp_path: Path) -> None:
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'absent.db'}", _env_file=None)
    with pytest.raises(ValueError, match="unknown"):
        CollectorService(
            settings, CollectorSettings(sources=("unknown",), _env_file=None), StoreRegistry()
        )
    assert not (tmp_path / "absent.db").exists()
