"""Backend cache permissions use fixture executors and never scrape retailers."""

import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from grocery_agent.acquisition.profiles import profiled_adapter
from grocery_agent.backend.api import create_app
from grocery_agent.backend.config import BackendSettings
from grocery_agent.backend.repository import ControlRepository
from grocery_agent.backend.worker import Worker
from grocery_agent.catalogue.profiles import AcquisitionProfile
from grocery_agent.config import Settings
from grocery_agent.contracts import Capabilities
from grocery_agent.meals.catalog import MealCatalog
from grocery_agent.persistence.database import create_database_engine
from grocery_agent.persistence.migrations import upgrade_database
from grocery_agent.recipes import RecipeRequest
from grocery_agent.stores.registry import default_registry
from tests.application_support import NOW

ROOT = Path(__file__).resolve().parents[1]


def effective(profile: AcquisitionProfile) -> AcquisitionProfile:
    return profiled_adapter(default_registry(), profile)[1]


@pytest.fixture
def backend_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    def no_prepare(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("API validation must not prepare or acquire recipe inputs")

    monkeypatch.setattr("grocery_agent.recipe_service.RecipeService.prepare", no_prepare)
    engine = create_database_engine(f"sqlite:///{tmp_path / 'control.db'}")
    upgrade_database(engine, "control")
    repo = ControlRepository(engine)
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'absent-offers.db'}",
        meal_config=ROOT / "config/meals.toml",
        _env_file=None,
    )
    token = repo.accept_invite(repo.invite("alice", NOW), NOW, 24)
    headers = {"Authorization": f"Bearer {token}"}

    def client(backend: BackendSettings) -> TestClient:
        return TestClient(create_app(settings, backend, engine=engine, clock=lambda: NOW))

    yield repo, settings, headers, client
    engine.dispose()


@pytest.mark.parametrize(
    ("policies", "default"),
    [
        (("cache-only",), "cache-only"),
        (("refresh", "local"), "refresh"),
        (("local", "cache-only"), "cache-only"),
    ],
)
def test_capabilities_defaults_and_scope_fallback(
    backend_fixture: Any, policies: tuple[str, ...], default: str
) -> None:
    repo, settings, headers, client = backend_fixture
    catalog = MealCatalog.load(settings.meal_config)
    backend = BackendSettings(cache_policies=policies)
    response = client(backend).get("/v1/capabilities", headers=headers)
    assert response.status_code == 200
    caps = response.json()
    assert caps["cache_policies"] == list(policies)
    assert caps["default_cache_policy"] == default
    assert caps["sources"] == [catalog.policy.source_id]
    assert caps["source_scopes"] == {catalog.policy.source_id: [catalog.policy.scope]}
    assert caps["combined_sources"] is False
    legacy = {k: v for k, v in caps.items() if k not in {"source_scopes", "combined_sources"}}
    assert Capabilities.model_validate(legacy).source_scopes == {}
    assert Capabilities.model_validate(legacy).combined_sources is False


@pytest.mark.parametrize("policy", ["local", "refresh", "no-cache"])
def test_acquiring_policies_require_explicit_profiles(backend_fixture: Any, policy: str) -> None:
    _, _, headers, client = backend_fixture
    app = client(BackendSettings(cache_policies=(policy, "cache-only")))
    response = app.post(
        "/v1/recipes", headers=headers, json={"provider": "template", "cache_policy": policy}
    )
    assert response.status_code == 422
    assert "configured profile" in response.json()["detail"]


@pytest.mark.parametrize("policy", ["local", "refresh", "no-cache", "cache-only"])
def test_combined_submission_is_acquisition_free(backend_fixture: Any, policy: str) -> None:
    repo, settings, headers, client = backend_fixture
    profiles = (
        effective(AcquisitionProfile(source_id="kupi")),
        effective(AcquisitionProfile(source_id="tesco")),
    )
    backend = BackendSettings(
        cache_policies=(policy,),
        source_scopes={"kupi": ("national",), "tesco": ("prague",)},
        acquisition_profiles=profiles,
    )
    app = client(backend)
    caps = app.get("/v1/capabilities", headers=headers).json()
    assert caps["sources"] == ["kupi", "tesco"]
    assert caps["combined_sources"] is True
    response = app.post(
        "/v1/recipes",
        headers=headers,
        json={
            "provider": "template",
            "source_ids": ["kupi", "tesco"],
            "profile_fingerprints": {p.source_id: p.fingerprint for p in profiles},
        },
    )
    assert response.status_code == 202, response.text
    claimed = repo.claim(NOW, 180, 3)
    assert claimed and claimed.request["cache_policy"] == policy
    assert claimed.inputs is None
    assert not Path(settings.database_url.removeprefix("sqlite:///")).exists()


@pytest.mark.parametrize(
    "payload",
    [
        {"source_ids": []},
        {"source_ids": ["kupi", "kupi"]},
        {"source_ids": ["rohlik"]},
        {"source_ids": ["kupi", "tesco"], "profile_fingerprint": "legacy"},
        {"source_ids": ["kupi"], "profile_fingerprints": {"tesco": "legacy"}},
    ],
)
def test_rejects_invalid_source_and_profile_selection(backend_fixture: Any, payload: Any) -> None:
    _, _, headers, client = backend_fixture
    app = client(BackendSettings(source_scopes={"kupi": ("national",), "tesco": ("prague",)}))
    response = app.post("/v1/recipes", headers=headers, json={"provider": "template", **payload})
    assert response.status_code == 422


def test_empty_mapping_does_not_authorize_profile_sources(backend_fixture: Any) -> None:
    _, _, headers, client = backend_fixture
    app = client(
        BackendSettings(
            cache_policies=("refresh",),
            acquisition_profiles=(AcquisitionProfile(source_id="tesco"),),
        )
    )
    assert (
        app.post(
            "/v1/recipes",
            headers=headers,
            json={
                "provider": "template",
                "source_ids": ["tesco"],
            },
        ).status_code
        == 422
    )
    assert (
        app.post("/v1/recipes", headers=headers, json={"provider": "template"}).status_code == 422
    )


def test_default_profile_refresh_and_legacy_cache_only(backend_fixture: Any) -> None:
    _, _, headers, client = backend_fixture
    app = client(
        BackendSettings(
            cache_policies=("cache-only", "refresh"),
            acquisition_profiles=(AcquisitionProfile(source_id="kupi"),),
        )
    )
    for extra in ({"cache_policy": "refresh"}, {"profile_fingerprint": "legacy"}, {}):
        response = app.post("/v1/recipes", headers=headers, json={"provider": "template", **extra})
        assert response.status_code == 202, response.text


def test_admin_refresh_requires_explicit_profile(backend_fixture: Any) -> None:
    repo, _, _, client = backend_fixture
    headers = {
        "Authorization": f"Bearer {repo.bootstrap('admin', NOW)}",
        "Idempotency-Key": "refresh",
    }
    payload = {"source_id": "kupi"}
    assert (
        client(BackendSettings(source_scopes={"kupi": ("kupi:locality:praha",)}))
        .post(
            "/v1/admin/refresh",
            headers=headers,
            json=payload,
        )
        .status_code
        == 422
    )
    app = client(BackendSettings(acquisition_profiles=(AcquisitionProfile(source_id="kupi"),)))
    assert app.get("/v1/capabilities", headers=headers).json()["refresh_allowed"] is True
    assert app.post("/v1/admin/refresh", headers=headers, json=payload).status_code == 202


def test_profile_selection_must_be_configured_and_unambiguous(backend_fixture: Any) -> None:
    _, _, headers, client = backend_fixture
    profiles = (
        effective(AcquisitionProfile(source_id="kupi", name="one")),
        effective(AcquisitionProfile(source_id="kupi", name="two")),
    )
    app = client(BackendSettings(cache_policies=("refresh",), acquisition_profiles=profiles))
    for payload in (
        {},
        {"profile_fingerprint": "a" * 24},
        {"profile_fingerprints": {"kupi": "legacy"}},
    ):
        assert (
            app.post(
                "/v1/recipes",
                headers=headers,
                json={
                    "provider": "template",
                    **payload,
                },
            ).status_code
            == 422
        )
    assert (
        app.post(
            "/v1/recipes",
            headers=headers,
            json={
                "provider": "template",
                "profile_fingerprints": {"kupi": profiles[0].fingerprint},
            },
        ).status_code
        == 202
    )


def test_permissions_ownership_and_quotas_still_apply(backend_fixture: Any) -> None:
    repo, _, headers, client = backend_fixture
    app = client(BackendSettings(max_pending_per_user=1))
    assert app.post("/v1/recipes", json={"provider": "template"}).status_code == 401
    first = app.post("/v1/recipes", headers=headers, json={"provider": "template"})
    assert first.status_code == 202
    assert (
        app.post("/v1/recipes", headers=headers, json={"provider": "template"}).status_code == 409
    )
    other = repo.accept_invite(repo.invite("bob", NOW), NOW, 24)
    other_headers = {"Authorization": f"Bearer {other}"}
    job = first.json()["job_id"]
    assert app.get(f"/v1/jobs/{job}", headers=other_headers).status_code == 404
    assert app.delete(f"/v1/jobs/{job}", headers=other_headers).status_code == 404
    assert (
        app.post(
            "/v1/admin/refresh",
            headers={**headers, "Idempotency-Key": "refresh"},
            json={"source_id": "kupi"},
        ).status_code
        == 403
    )


@pytest.mark.parametrize(
    "config",
    [
        {"cache_policies": ()},
        {"cache_policies": ("refresh", "refresh")},
        {"source_scopes": {"kupi": ()}},
        {"source_scopes": {"kupi": ("",)}},
        {"source_scopes": {"kupi": ("national", "national")}},
        {
            "source_scopes": {"kupi": ("national",)},
            "acquisition_profiles": (AcquisitionProfile(source_id="tesco"),),
        },
        {
            "source_scopes": {"kupi": ("national",)},
            "acquisition_profiles": (AcquisitionProfile(source_id="kupi", scope="elsewhere"),),
        },
    ],
)
def test_configuration_rejects_invalid_permissions(config: Any) -> None:
    with pytest.raises(ValidationError):
        BackendSettings(**config)


@pytest.mark.asyncio
async def test_worker_prepares_off_loop_and_reuses_pinned_combined_inputs(
    backend_fixture: Any,
) -> None:
    repo, settings, headers, _ = backend_fixture
    user = repo.authenticate(headers["Authorization"].removeprefix("Bearer "), NOW)
    request = RecipeRequest(
        provider="template",
        cache_policy="cache-only",
        source_ids=("kupi", "tesco"),
        profile_fingerprints={"kupi": "a" * 24, "tesco": "b" * 24},
    )
    pinned = {
        "collections": [
            {"source_id": "kupi", "run_id": "one", "coverage_complete": True},
            {"source_id": "tesco", "run_id": "two", "coverage_complete": True},
        ],
        "catalogue_snapshot": {"run_ids": ["one", "two"], "complete": True},
        "effective_request": request.model_dump(mode="json"),
    }
    event_thread = threading.get_ident()

    class Executor:
        prepares = 0
        executed: list[dict[str, Any]] = []

        def prepare(self, request: RecipeRequest) -> dict[str, Any]:
            assert threading.get_ident() != event_thread
            self.prepares += 1
            return pinned

        def execute(
            self, request: RecipeRequest, inputs: dict[str, Any]
        ) -> tuple[dict[str, Any], str]:
            assert threading.get_ident() != event_thread
            self.executed.append(inputs)
            return {"status": "ok", **inputs}, "<p>Fixture</p>"

    executor = Executor()
    backend = BackendSettings(source_scopes={"kupi": ("national",), "tesco": ("prague",)})
    repo.submit(user.user_id, request.model_dump(mode="json"), "fresh", NOW)
    worker = Worker(repo, executor, settings, backend, lambda: NOW)
    assert await worker.once()
    assert executor.prepares == 1
    recovered = repo.submit(user.user_id, request.model_dump(mode="json"), "recovered", NOW)
    claim = repo.claim(NOW, 10, 3)
    assert claim
    repo.pin(claim, pinned, NOW)
    worker.clock = lambda: NOW + timedelta(seconds=11)
    assert await worker.once()
    assert executor.prepares == 1
    assert executor.executed == [pinned, pinned]
    assert repo.result(user.user_id, recovered.job_id)[0]["collections"] == pinned["collections"]
    assert not await worker.once()
