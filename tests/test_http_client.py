import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from grocery_agent.backend.api import create_app
from grocery_agent.backend.config import BackendSettings
from grocery_agent.backend.repository import ControlRepository
from grocery_agent.catalogue.models import CatalogueQuery
from grocery_agent.catalogue.repository import SQLAlchemyCatalogueRepository
from grocery_agent.config import Settings
from grocery_agent.http_client import AsyncGroceryClient, BackendError, GroceryClient
from grocery_agent.persistence.database import create_database_engine
from grocery_agent.persistence.migrations import upgrade_database
from grocery_agent.persistence.repository import SQLAlchemyOfferRepository
from grocery_agent.persistence.snapshots import FileSnapshotStore
from grocery_agent.recipes import RecipeRequest
from tests.application_support import NOW, complete_batch
from tests.test_meals import groceries

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_password_login_and_profile_refresh_sync_async() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/auth/login":
            assert "authorization" not in request.headers
            assert json.loads(request.content)["password"] == "long fixture password"
            return httpx.Response(200, json={"token": "private-token", "session_mode": "bearer"})
        assert request.headers["Authorization"] == "Bearer private-token"
        assert request.headers["Idempotency-Key"] == "refresh-key"
        assert json.loads(request.content) == {"source_id": "kupi", "profile_fingerprint": "a" * 24}
        return httpx.Response(
            202,
            json={
                "job_id": "refresh",
                "kind": "refresh",
                "status": "queued",
                "phase": "queued",
                "attempts": 0,
                "created_at": NOW.isoformat(),
                "updated_at": NOW.isoformat(),
                "error_code": None,
            },
        )

    transport = httpx.MockTransport(handle)
    with GroceryClient("http://local", transport=transport) as client:
        client.login("alice", "long fixture password")
        assert (
            client.refresh("kupi", idempotency_key="refresh-key", profile_fingerprint="a" * 24).kind
            == "refresh"
        )
    async with AsyncGroceryClient("http://local", transport=transport) as client:
        await client.login("alice", "long fixture password")
        job = await client.refresh(
            "kupi", idempotency_key="refresh-key", profile_fingerprint="a" * 24
        )
        assert job.kind == "refresh"
    assert len(seen) == 4


@pytest.mark.asyncio
async def test_profile_discovery_sync_and_async_clients() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.path == "/v1/collections"
        assert request.headers["Authorization"] == "Bearer token"
        return httpx.Response(200, json=[{"profile_fingerprint": "a" * 24}])

    transport = httpx.MockTransport(handle)
    with GroceryClient("http://local", "token", transport=transport) as client:
        assert client.collections("kupi")[0]["profile_fingerprint"] == "a" * 24
    async with AsyncGroceryClient("http://local", "token", transport=transport) as client:
        assert (await client.collections())[0]["profile_fingerprint"] == "a" * 24
    assert seen[0].url.params["source"] == "kupi"
    assert "source" not in seen[1].url.params


def test_sync_client_exact_payload_idempotency_errors_and_no_redirects() -> None:
    seen: list[httpx.Request] = []
    job = {
        "job_id": "request-id",
        "kind": "recipe",
        "status": "queued",
        "phase": "queued",
        "attempts": 0,
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
        "error_code": None,
    }

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["Authorization"] == "Bearer secret-token"
        if request.url.path == "/v1/recipes":
            body = json.loads(request.content)
            assert "cache_policy" not in body and body["max_cost_per_serving_czk"] == "12.3400"
            return httpx.Response(202, json=job)
        return httpx.Response(307, headers={"location": "https://other.example/private"})

    with GroceryClient(
        "http://local", "secret-token", transport=httpx.MockTransport(handle)
    ) as client:
        request = RecipeRequest(provider="template", max_cost_per_serving_czk="12.3400")
        assert client.submit(request).status == "queued"
        client.submit(request)
        assert seen[0].headers["Idempotency-Key"] == seen[1].headers["Idempotency-Key"]
        with pytest.raises(BackendError) as error:
            client.job("request-id")
        assert error.value.status == 307 and len(seen) == 3
        with pytest.raises(ValueError):
            client.wait("request-id", max_wait_seconds=0)


def test_remote_cli_uses_shared_client_and_advertised_defaults(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    from grocery_agent.apps import recipes
    from grocery_agent.contracts import Capabilities, JobView

    seen = []

    class FakeClient:
        def __init__(self, url: str, token: str) -> None:
            assert url == "http://backend.test" and token == "private-session"

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def capabilities(self) -> Capabilities:
            return Capabilities(
                providers=("template",),
                default_provider="template",
                models={},
                cache_policies=("cache-only",),
                default_cache_policy="cache-only",
                sources=("kupi",),
                scope="praha",
                ingredients=("rice",),
                meal_styles=("main",),
                refresh_allowed=False,
                email_enabled=False,
                simplex_enabled=False,
            )

        def submit(self, request: RecipeRequest) -> JobView:
            seen.append(request)
            return JobView(
                job_id="job",
                kind="recipe",
                status="queued",
                phase="queued",
                attempts=0,
                created_at=NOW,
                updated_at=NOW,
                error_code=None,
            )

    monkeypatch.setattr(recipes, "GroceryClient", FakeClient)
    monkeypatch.setenv("GROCERY_API_TOKEN", "private-session")
    assert (
        recipes.main(
            [
                "--api-url",
                "http://backend.test",
                "--have",
                "rice=500g",
                "--budget",
                "80.0000",
                "--no-wait",
            ]
        )
        == 0
    )
    request = seen[0]
    assert request.provider == "template" and request.cache_policy == "cache-only"
    assert request.pantry == {"rice": "500g"} and request.max_cost_per_serving_czk == 80
    output = capsys.readouterr()
    assert json.loads(output.out)["job_id"] == "job" and "private-session" not in output.out


@pytest.mark.asyncio
async def test_async_client_backend_contracts_ownership_and_computed_offers(
    engine: Any,
    repository: SQLAlchemyOfferRepository,
    snapshots: FileSnapshotStore,
    candidate: dict[str, Any],
    tmp_path: Path,
) -> None:
    run = complete_batch(
        repository, snapshots, [row.offer for row in groceries(candidate)], source="kupi"
    )
    SQLAlchemyCatalogueRepository(engine).publish(run.run_id)
    control = create_database_engine(f"sqlite:///{tmp_path / 'client-control.db'}")
    upgrade_database(control, "control")
    repo = ControlRepository(control)
    token = repo.bootstrap("admin", NOW)
    other = repo.accept_invite(repo.invite("bob", NOW), NOW, 24)
    settings = Settings(
        database_url=str(engine.url), meal_config=ROOT / "config/meals.toml", _env_file=None
    )
    app = create_app(
        settings, BackendSettings(providers=("template",)), engine=control, clock=lambda: NOW
    )
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with AsyncGroceryClient(
                "http://backend.test", token, transport=transport
            ) as client:
                supported = await client.capabilities()
                assert supported.default_provider == "template"
                assert supported.recipe_request_schema["properties"]["servings"]["maximum"] == 20
                assert supported.cache_policies == ("cache-only",)
                receipt = await client.submit(
                    RecipeRequest(provider="template"), idempotency_key="browser-key"
                )
                assert (await client.jobs()).items[0].job_id == receipt.job_id
                page = await client.offers(
                    CatalogueQuery(source_id="kupi", unit="kg", retailers=("billa",), limit=2)
                )
                assert page.total == 6 and len(page.items) == 2
                assert page.items[0].offer.unit_price.amount == 20
                assert await client.me()
                async with AsyncGroceryClient(
                    "http://backend.test", other, transport=transport
                ) as bob:
                    assert not (await bob.jobs()).items
                    with pytest.raises(BackendError) as denied:
                        await bob.job(receipt.job_id)
                    assert denied.value.status == 404
                await client.cancel(receipt.job_id)
                assert (await client.wait(receipt.job_id)).status == "cancelled"
                with pytest.raises(BackendError) as unavailable:
                    await client.result(receipt.job_id)
                assert unavailable.value.status == 409
                await client.logout()
                with pytest.raises(BackendError) as revoked:
                    await client.me()
                assert revoked.value.status == 401
    finally:
        control.dispose()


@pytest.mark.asyncio
async def test_static_assets_openapi_and_optional_website(tmp_path: Path) -> None:
    control = create_database_engine(f"sqlite:///{tmp_path / 'website-control.db'}")
    upgrade_database(control, "control")
    settings = Settings(meal_config=ROOT / "config/meals.toml", _env_file=None)
    try:
        for enabled in (True, False):
            app = create_app(settings, BackendSettings(website_enabled=enabled), engine=control)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app), base_url="http://site"
                ) as client:
                    index = await client.get("/")
                    assert index.status_code == (200 if enabled else 404)
                    if enabled:
                        assert "script-src 'self'" in index.headers["Content-Security-Policy"]
                        assert (await client.get("/assets/app.js")).status_code == 200
                        assert (await client.get("/assets/../../.env")).status_code != 200
                    schema = (await client.get("/openapi.json")).json()
                    assert "Capabilities" in schema["components"]["schemas"]
                    assert "/v1/jobs" in schema["paths"]
                    assert schema["paths"]["/v1/jobs"]["get"]["security"] == [{"BearerSession": []}]
                    assert "security" not in schema["paths"]["/v1/invitations/accept"]["post"]
    finally:
        control.dispose()
