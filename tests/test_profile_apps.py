"""Offline checks for independent app startup and profile publication boundaries."""

import json
from collections.abc import AsyncIterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from grocery_agent.application.runtime import run_acquisition
from grocery_agent.apps import collect, scrape
from grocery_agent.catalogue.models import CatalogueQuery
from grocery_agent.catalogue.profiles import AcquisitionProfile
from grocery_agent.catalogue.repository import SQLAlchemyCatalogueRepository
from grocery_agent.collector.config import (
    CollectorSettings,
    CollectSettings,
    ScrapeSettings,
    select_profiles,
)
from grocery_agent.collector.service import CollectorService
from grocery_agent.config import Settings
from grocery_agent.models.common import utc_now
from grocery_agent.persistence.database import create_database_engine
from grocery_agent.pipeline.results import ScrapeResult
from grocery_agent.stores.base import AcquisitionItem, AdapterContext
from grocery_agent.stores.mock.adapter import MockStore
from grocery_agent.stores.registry import StoreRegistry
from grocery_agent.workflow import writer_lock


@pytest.fixture
def registry() -> StoreRegistry:
    value = StoreRegistry()
    value.register("mock", MockStore)
    return value


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'offers.db'}",
        snapshot_dir=tmp_path / "snapshots",
        report_dir=tmp_path / "reports",
        lock_path=tmp_path / "writer.lock",
        _env_file=None,
    )


def test_independent_defaults_and_missing_default_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "collector.toml").write_text('sources = ["unknown"]\n')
    monkeypatch.setenv("GROCERY_COLLECTOR_CONFIG", str(tmp_path / "collector.toml"))
    assert ScrapeSettings().profiles == (AcquisitionProfile(source_id="kupi"),)
    options = CollectSettings()
    assert options.daily_at == "02:00" and options.timezone == "Europe/Prague"
    assert options.schedule().profiles == options.profiles
    seen: list[ScrapeSettings] = []

    def factory(settings: Settings, options: ScrapeSettings) -> CollectorService:
        seen.append(options)
        raise ValueError("stop before storage")

    monkeypatch.setattr(scrape, "create_service", factory)
    assert scrape.main([]) == 1
    assert seen[0].profiles == options.profiles
    assert not (tmp_path / "data").exists()


def test_config_environment_and_cli_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "collect.toml"
    path.write_text(
        'daily_at = "01:00"\ntimezone = "UTC"\nrun_on_startup = false\n'
        '[[profiles]]\nsource_id = "mock"\nname = "default"\n'
    )
    (tmp_path / ".env").write_text(
        "GROCERY_DATABASE_URL=sqlite:///offers.db\nGROCERY_COLLECT_DAILY_AT=03:00\n"
        'GROCERY_SCRAPE_PROFILES=[{"source_id":"mock","name":"dotenv"}]\n'
    )
    assert CollectSettings.load(path).daily_at == "03:00"
    monkeypatch.setenv("GROCERY_COLLECT_DAILY_AT", "04:00")
    monkeypatch.setenv("GROCERY_COLLECT_TIMEZONE", "Europe/Prague")
    monkeypatch.setenv("GROCERY_COLLECTOR_DAILY_AT", "06:00")
    assert CollectSettings.load(path).daily_at == "04:00"
    assert ScrapeSettings().profiles[0].name == "dotenv"
    monkeypatch.setenv("GROCERY_SCRAPE_PROFILES", '[{"source_id":"mock","name":"env"}]')
    assert ScrapeSettings().profiles[0].name == "env"
    seen: list[CollectSettings] = []

    def factory(settings: Settings, options: CollectSettings) -> CollectorService:
        seen.append(options)
        raise ValueError("private-password-never-print")

    monkeypatch.setattr(collect, "create_service", factory)
    assert (
        collect.main(["--config", str(path), "--once", "--daily-at", "05:00", "--timezone", "UTC"])
        == 1
    )
    assert seen[0].schedule().daily_at == "05:00"
    assert seen[0].schedule().timezone == "UTC"
    assert not seen[0].run_on_startup
    assert "private-password" not in capsys.readouterr().out
    seen.clear()
    assert collect.main(["--config", str(path), "--daily-at", "25:00"]) == 1
    assert not seen


def test_profile_selection_and_source_default_parity() -> None:
    profiles = (
        AcquisitionProfile(source_id="mock"),
        AcquisitionProfile(source_id="mock", name="narrow", search=("rice",)),
        AcquisitionProfile(source_id="kupi"),
    )
    assert select_profiles(profiles) == profiles
    assert select_profiles(profiles, sources=["mock"]) == select_profiles(
        profiles, names=["mock:default"]
    )
    assert select_profiles(profiles, names=["narrow"]) == (profiles[1],)
    assert select_profiles(profiles, sources=["tesco"])[0].source_id == "tesco"
    for names, sources in (
        (["default"], []),
        (["missing"], []),
        (["narrow", "mock:narrow"], []),
        ([], ["mock", "mock"]),
        (["narrow"], ["kupi"]),
    ):
        with pytest.raises(ValueError):
            select_profiles(profiles, names, sources)


def test_config_rejects_ambiguous_identities_and_global_settings(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        ScrapeSettings(profiles=(), _env_file=None)
    with pytest.raises(ValidationError):
        ScrapeSettings(
            profiles=(
                AcquisitionProfile(source_id="mock", name="same"),
                AcquisitionProfile(source_id="mock", name="same", search=("rice",)),
            ),
            _env_file=None,
        )
    path = tmp_path / "scrape.toml"
    path.write_text('database_url = "sqlite:///secret.db"\n')
    with pytest.raises(ValueError, match="unknown"):
        ScrapeSettings.load(path)


@pytest.mark.parametrize(
    "profile",
    [
        AcquisitionProfile(source_id="unknown"),
        AcquisitionProfile(source_id="mock", categories=("unsupported",)),
        AcquisitionProfile(source_id="mock", options={"max_pages": 10}),
    ],
)
def test_profile_preflight_creates_no_storage(
    profile: AcquisitionProfile, settings: Settings, registry: StoreRegistry, tmp_path: Path
) -> None:
    with pytest.raises(ValueError):
        scrape.create_service(
            settings, ScrapeSettings(profiles=(profile,), _env_file=None), registry
        )
    assert not (tmp_path / "offers.db").exists()
    assert not settings.lock_path.exists()
    assert not settings.snapshot_dir.exists()


def test_effective_default_kupi_identity(settings: Settings) -> None:
    from grocery_agent.acquisition.profiles import effective_profile
    from grocery_agent.stores.kupi.adapter import KupiAdapter
    from grocery_agent.stores.kupi.config import KupiSettings

    registry = StoreRegistry()
    registry.register(
        "kupi", lambda: KupiAdapter(KupiSettings(category_slugs=("pecivo",), _env_file=None))
    )
    service = scrape.create_service(settings, ScrapeSettings(_env_file=None), registry)
    adapter, profile = service._profiled_adapters[0]
    assert profile == effective_profile(
        KupiAdapter(KupiSettings(category_slugs=("pecivo",), _env_file=None))
    )
    assert profile.categories == ("pecivo",)
    assert profile.options["max_pages"] == 100
    assert profile.fingerprint != AcquisitionProfile(source_id="kupi").fingerprint
    assert adapter.source_id == "kupi"


@pytest.mark.asyncio
async def test_shared_acquisition_applies_raw_adapter_profile(settings: Settings) -> None:
    profile = AcquisitionProfile(source_id="mock", search=("kuřecí",))
    (result,) = await run_acquisition(settings, [MockStore()], profiles=[profile])
    assert result.status == "success" and result.accepted == 1
    assert result.profile_fingerprint == profile.fingerprint
    engine = create_database_engine(settings.database_url)
    try:
        cache = SQLAlchemyCatalogueRepository(engine)
        page = cache.search(
            CatalogueQuery(source_id="mock", profile_fingerprint=profile.fingerprint), utc_now()
        )
        assert page.total == 1 and page.state.coverage_complete
        assert "Kuřecí" in page.items[0].offer.product.name
        assert page.state.actual_scopes
        assert cache.collections()[0]["profile"]["search"] == ["kuřecí"]
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_collector_runs_every_profile_under_one_lock(
    settings: Settings, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    from grocery_agent.collector import service as module

    profiles = (
        AcquisitionProfile(source_id="mock", name="first"),
        AcquisitionProfile(source_id="mock", name="second"),
    )
    service = CollectorService(
        settings, CollectorSettings(sources=("mock",), profiles=profiles, _env_file=None), registry
    )
    seen: list[AcquisitionProfile] = []
    lock_entries = 0
    locked = False
    original_lock = writer_lock

    @contextmanager
    def lock(path: Path) -> Any:
        nonlocal lock_entries, locked
        lock_entries += 1
        with original_lock(path):
            locked = True
            try:
                yield
            finally:
                locked = False

    async def run(
        settings: Settings, adapters: Any, *, profiles: Any = None
    ) -> tuple[ScrapeResult, ...]:
        assert locked
        assert len(adapters) == len(profiles) == 1
        seen.extend(profiles)
        return (ScrapeResult(source_id="mock", status="success"),)

    monkeypatch.setattr(module, "run_acquisition", run)
    monkeypatch.setattr(module, "writer_lock", lock)
    assert len(await service.collect_once()) == 2
    assert seen == list(profiles)
    assert lock_entries == 1 and not locked


@pytest.mark.asyncio
async def test_publication_exception_does_not_skip_later_profiles(
    settings: Settings, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    from grocery_agent.collector import service as module

    profiles = tuple(AcquisitionProfile(source_id="mock", name=name) for name in ("a", "b", "c"))
    service = scrape.create_service(
        settings, ScrapeSettings(profiles=profiles, _env_file=None), registry
    )
    seen: list[str] = []

    async def run(
        settings: Settings, adapters: Any, *, profiles: Any = None
    ) -> tuple[ScrapeResult, ...]:
        seen.append(profiles[0].name)
        if profiles[0].name == "b":
            raise ValueError("publication-secret")
        return (ScrapeResult(source_id="mock", status="success"),)

    monkeypatch.setattr(module, "run_acquisition", run)
    with pytest.raises(ValueError, match="incomplete"):
        await service.collect_once()
    assert seen == ["a", "b", "c"]


@pytest.mark.parametrize("app", [scrape, collect])
def test_app_offline_json_and_source_profile_parity(
    app: Any,
    settings: Settings,
    registry: StoreRegistry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GROCERY_DATABASE_URL", settings.database_url)
    monkeypatch.setenv("GROCERY_SNAPSHOT_DIR", str(settings.snapshot_dir))
    monkeypatch.setenv("GROCERY_LOCK_PATH", str(settings.lock_path))
    monkeypatch.setenv("GROCERY_REPORT_DIR", str(settings.report_dir))
    path = tmp_path / "app.toml"
    path.write_text('[[profiles]]\nsource_id = "mock"\nname = "default"\n')
    factory = app.create_service
    monkeypatch.setattr(
        app, "create_service", lambda settings, options: factory(settings, options, registry)
    )
    base = ["--config", str(path), *(["--once"] if app is collect else [])]
    assert app.main([*base, "--source", "mock"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert app.main([*base, "--profile", "mock:default"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert first["accepted"] == 5 and second["changed"] == 0
    assert first["profile_fingerprint"] == second["profile_fingerprint"]
    assert first["profile_fingerprint"] != "legacy"
    assert second["acquisition_profile"]["name"] == "default"
    assert not settings.report_dir.exists() and not (tmp_path / "control.db").exists()


class FailingStore(MockStore):
    async def fetch_offers(self, context: AdapterContext) -> AsyncIterator[AcquisitionItem]:
        async for item in super().fetch_offers(context):
            yield item
        raise ValueError("fixture failure after accepted items")


@pytest.mark.parametrize("app", [scrape, collect])
def test_failed_acquisition_exits_one_with_profile_json(
    app: Any,
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GROCERY_DATABASE_URL", settings.database_url)
    monkeypatch.setenv("GROCERY_SNAPSHOT_DIR", str(settings.snapshot_dir))
    monkeypatch.setenv("GROCERY_LOCK_PATH", str(settings.lock_path))
    registry = StoreRegistry()
    registry.register("mock", FailingStore)
    factory = app.create_service
    monkeypatch.setattr(
        app, "create_service", lambda settings, options: factory(settings, options, registry)
    )
    args = ["--source", "mock", *(["--once"] if app is collect else [])]
    assert app.main(args) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "failed" and result["accepted"] == 5
    assert result["profile_fingerprint"] == AcquisitionProfile(source_id="mock").fingerprint
    assert not (tmp_path / "data" / "reports").exists()


@pytest.mark.asyncio
async def test_scheduled_app_uses_existing_serve(
    settings: Settings, registry: StoreRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = collect.create_service(
        settings,
        CollectSettings(profiles=(AcquisitionProfile(source_id="mock"),), _env_file=None),
        registry,
    )
    served = False

    async def serve() -> None:
        nonlocal served
        served = True

    monkeypatch.setattr(service, "serve", serve)
    assert await collect.run(service, False) == 0
    assert served


@pytest.mark.asyncio
async def test_partial_declared_profile_reports_failure_and_continues(
    settings: Settings, registry: StoreRegistry, capsys: pytest.CaptureFixture[str]
) -> None:
    profiles = (
        AcquisitionProfile(source_id="mock", name="partial", coverage="partial"),
        AcquisitionProfile(source_id="mock", name="complete"),
    )
    service = scrape.create_service(
        settings, ScrapeSettings(profiles=profiles, _env_file=None), registry
    )
    assert await scrape.run_once(service) == 1
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(rows) == 2
    by_profile = {row["profile_fingerprint"]: row for row in rows}
    assert by_profile[profiles[0].fingerprint]["status"] == "failed"
    assert by_profile[profiles[1].fingerprint]["status"] == "success"
    assert by_profile[profiles[1].fingerprint]["accepted"] == 5


@pytest.mark.asyncio
async def test_native_kupi_profile_runs_and_publishes_resolved_options(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from grocery_agent.catalogue.repository import SQLAlchemyCatalogueRepository
    from grocery_agent.persistence.database import create_database_engine
    from tests.kupi_support import adapter, fixture_response

    client_type = httpx.AsyncClient

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return client_type(transport=httpx.MockTransport(fixture_response), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    registry = StoreRegistry()
    registry.register("kupi", adapter)
    service = scrape.create_service(settings, ScrapeSettings(_env_file=None), registry)
    (result,) = await service.collect_once()
    assert result.status == "success" and result.accepted > 54
    assert result.acquisition_profile is not None
    assert result.acquisition_profile["categories"] == ["ovoce-a-zelenina"]
    assert result.acquisition_profile["options"]["listing_url"].endswith("/ovoce-a-zelenina")
    assert result.profile_fingerprint == service._profiled_adapters[0][1].fingerprint
    engine = create_database_engine(settings.database_url, read_only=True)
    try:
        state = SQLAlchemyCatalogueRepository(engine).state(
            "kupi", utc_now(), profile_fingerprint=result.profile_fingerprint
        )
        assert state.run_id == result.run_id
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_multiple_profile_failure_preserves_previous_heads(
    settings: Settings, registry: StoreRegistry
) -> None:
    from grocery_agent.catalogue.repository import SQLAlchemyCatalogueRepository
    from grocery_agent.persistence.database import create_database_engine

    profiles = (
        AcquisitionProfile(source_id="mock", name="all"),
        AcquisitionProfile(source_id="mock", name="milk", search=("mléko",)),
    )
    options = ScrapeSettings(profiles=profiles, _env_file=None)
    service = scrape.create_service(settings, options, registry)
    first = await service.collect_once()
    assert all(result.status == "success" for result in first)
    calls = 0

    def factory() -> MockStore:
        nonlocal calls
        calls += 1
        return FailingStore() if calls == 2 else MockStore()

    failing_registry = StoreRegistry()
    failing_registry.register("mock", factory)
    second = await scrape.create_service(settings, options, failing_registry).collect_once()
    assert second[0].status == "success" and second[1].status == "failed"
    engine = create_database_engine(settings.database_url, read_only=True)
    try:
        repository = SQLAlchemyCatalogueRepository(engine)
        now = utc_now()
        assert (
            repository.state("mock", now, profile_fingerprint=profiles[0].fingerprint).run_id
            == second[0].run_id
        )
        assert (
            repository.state("mock", now, profile_fingerprint=profiles[1].fingerprint).run_id
            == first[1].run_id
        )
    finally:
        engine.dispose()
