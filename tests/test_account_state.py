import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from grocery_agent.account_models import (
    PantryItem,
    PantryPut,
    PresetCreate,
    PresetUpdate,
    SettingsPatch,
)
from grocery_agent.application.parameters import StockOverride
from grocery_agent.backend.api import create_app
from grocery_agent.backend.config import BackendSettings
from grocery_agent.backend.repository import ControlRepository
from grocery_agent.backend.user_state import UserStateRepository, mount_user_state
from grocery_agent.config import Settings
from grocery_agent.http_client import AsyncGroceryClient, BackendError, GroceryClient
from grocery_agent.persistence.control.schema import AccountStateRow, ProfileRow
from grocery_agent.persistence.database import create_database_engine
from grocery_agent.persistence.migrations import upgrade_database
from grocery_agent.recipes import RecipeRequest
from tests.application_support import NOW

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def accounts(tmp_path: Path) -> Any:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'control.db'}")
    upgrade_database(engine, "control")
    control = ControlRepository(engine)
    token = control.bootstrap("admin", NOW)
    other_token = control.accept_invite(control.invite("alice", NOW), NOW, 24)
    user = control.authenticate(token, NOW)
    other = control.authenticate(other_token, NOW)
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'absent-offers.db'}",
        meal_config=ROOT / "config/meals.toml",
        _env_file=None,
    )
    app = create_app(settings, engine=engine, clock=lambda: NOW)
    yield control, UserStateRepository(control), app, user, other, token, other_token
    engine.dispose()


@pytest.mark.asyncio
async def test_settings_defaults_partial_patch_conflicts_and_persistence(accounts: Any) -> None:
    control, repo, app, user, _, token, other_token = accounts
    async with AsyncGroceryClient(
        "http://test", token, transport=httpx.ASGITransport(app)
    ) as client:
        assert (await client.settings()).model_dump() == {
            "revision": 0,
            "ui_language": None,
            "recipe_language": None,
        }
        changed = await client.patch_settings(SettingsPatch(expected_revision=0, ui_language="cs"))
        assert changed.revision == 1 and changed.recipe_language is None
        changed = await client.patch_settings(
            SettingsPatch(expected_revision=1, recipe_language="en")
        )
        assert changed.ui_language == "cs" and changed.recipe_language == "en"
        with pytest.raises(BackendError) as conflict:
            await client.patch_settings(SettingsPatch(expected_revision=1, ui_language="en"))
        assert conflict.value.status == 409
        changed = await client.patch_settings(SettingsPatch(expected_revision=2, ui_language=None))
        assert changed.ui_language is None and changed.recipe_language == "en"
        assert (await client.pantry()).revision == 0
    # A fresh repository reads the committed nullable values, not process-local state.
    assert (
        UserStateRepository(control).settings(user.user_id)
        == repo.settings(user.user_id)
        == {
            "revision": 3,
            "ui_language": None,
            "recipe_language": "en",
        }
    )
    async with AsyncGroceryClient(
        "http://test", other_token, transport=httpx.ASGITransport(app)
    ) as client:
        assert (await client.settings()).revision == 0
    with control.sessions() as session:
        assert len(session.scalars(select(AccountStateRow)).all()) == 1


@pytest.mark.parametrize("grams", [0, 1, 1.2, True, "0", "-1", "1e3", "NaN", "inf", "1kg", ""])
def test_pantry_rejects_nonpositive_or_nondecimal_string_grams(grams: Any) -> None:
    with pytest.raises(ValidationError):
        PantryItem(grams=grams)


@pytest.mark.parametrize(
    "grams,canonical",
    [
        ("0.0000010000", "0.000001"),
        ("00012.123456000", "12.123456"),
        ("9999999999.999999000", "9999999999.999999"),
        ("1.0000000000", "1"),
    ],
)
def test_pantry_precision_matches_stock_override_after_canonicalization(
    grams: str,
    canonical: str,
) -> None:
    item = PantryItem(grams=grams)
    assert item.grams == canonical
    assert StockOverride(grams=item.grams).grams == StockOverride(grams=grams).grams


@pytest.mark.parametrize("grams", ["0.0000001", "1.1234567", "12345678901.123456"])
def test_pantry_rejects_recipe_stock_precision_overflow(grams: str) -> None:
    with pytest.raises(ValidationError):
        StockOverride(grams=grams)
    with pytest.raises(ValidationError):
        PantryItem(grams=grams)


@pytest.mark.asyncio
async def test_pantry_precision_api_rejection_keeps_revision_and_saved_stock(accounts: Any) -> None:
    _, _, app, _, _, token, _ = accounts
    async with AsyncGroceryClient(
        "http://test", token, transport=httpx.ASGITransport(app)
    ) as client:
        for amount in ("0.0000001", "1.1234567", "12345678901.123456"):
            response = await client.http.put(
                "/v1/me/pantry",
                json={
                    "expected_revision": 0,
                    "items": {"rice": {"grams": amount}},
                },
            )
            assert response.status_code == 422
            assert (await client.pantry()).revision == 0
        response = await client.http.put(
            "/v1/me/pantry",
            json={
                "expected_revision": 0,
                "items": {"rice": {"grams": "1.1234560000"}},
            },
        )
        assert response.status_code == 200
        assert response.json()["items"]["rice"]["grams"] == "1.123456"
        submitted = await client.http.post(
            "/v1/recipes",
            json={
                "provider": "template",
                "pantry": {"rice": "1.123456g"},
            },
        )
        assert submitted.status_code == 202, submitted.text


@pytest.mark.asyncio
async def test_pantry_canonical_grams_enough_limit_replace_and_owner_isolation(
    accounts: Any,
) -> None:
    _, repo, app, user, other, token, _ = accounts
    async with AsyncGroceryClient(
        "http://test", token, transport=httpx.ASGITransport(app)
    ) as client:
        state = await client.put_pantry(
            PantryPut(
                expected_revision=0,
                items={
                    "rice": PantryItem(grams="00012.3400", use_first=True),
                    "salt": PantryItem(grams=None),
                    "exact": PantryItem(grams="9999999999.123456000"),
                },
            )
        )
        assert state.items["rice"].grams == "12.34" and state.items["rice"].use_first
        assert state.items["salt"].grams is None
        assert state.items["exact"].grams == "9999999999.123456"
        assert (await client.pantry()) == state
        with pytest.raises(BackendError) as conflict:
            await client.put_pantry(PantryPut(expected_revision=0, items={}))
        assert conflict.value.status == 409
        assert repo.pantry(other.user_id).items == {}
        assert repo.settings(user.user_id)["revision"] == 0
        replaced = await client.put_pantry(PantryPut(expected_revision=1, items={}))
        assert replaced.revision == 2 and replaced.items == {}
        response = await client.http.put(
            "/v1/me/pantry",
            json={
                "expected_revision": 2,
                "items": {f"item-{n}": {"grams": None, "use_first": True} for n in range(201)},
            },
        )
        assert response.status_code == 422
        assert (await client.pantry()).revision == 2
    assert (
        len(
            PantryPut(
                expected_revision=0,
                items={str(n): PantryItem(grams=None, use_first=True) for n in range(200)},
            ).items
        )
        == 200
    )


@pytest.mark.asyncio
async def test_account_requests_require_auth_and_strict_inputs(accounts: Any) -> None:
    _, _, app, _, _, token, _ = accounts
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        for path in ("/v1/me/settings", "/v1/me/pantry", "/v1/me/presets"):
            assert (await client.get(path)).status_code == 401
        client.headers["Authorization"] = f"Bearer {token}"
        for body in (
            {"expected_revision": 0, "ui_language": "de"},
            {"expected_revision": True, "ui_language": "cs"},
            {"expected_revision": "0", "ui_language": "en"},
            {"ui_language": "cs"},
            {"expected_revision": 0, "other": True},
        ):
            assert (await client.patch("/v1/me/settings", json=body)).status_code == 422
        assert (
            await client.put(
                "/v1/me/pantry",
                json={
                    "expected_revision": 0,
                    "items": {"rice": {"grams": "1", "use_first": "yes"}},
                },
            )
        ).status_code == 422


@pytest.mark.asyncio
async def test_preset_crud_revisions_conflicts_validation_and_foreign_404(accounts: Any) -> None:
    _, _, app, _, _, token, other_token = accounts
    transport = httpx.ASGITransport(app)
    async with AsyncGroceryClient("http://test", token, transport=transport) as client:
        saved = await client.create_preset(
            PresetCreate(
                name="Večeře",
                parameters={
                    "provider": "template",
                    "language": "cs",
                    "servings": 3,
                },
            )
        )
        assert saved.revision == 1 and not saved.stale
        assert saved.parameters["cache_policy"] == "cache-only"
        assert {"pantry", "use_first", "request_id"}.isdisjoint(saved.parameters)
        assert (await client.presets()).total == 1
        assert (await client.preset(saved.id)) == saved
        for name, params in (
            ("Večeře", {"provider": "template"}),
            ("invalid", {"provider": "ollama", "model": "not-configured"}),
            ("invalid", {"provider": "template", "source_ids": ["not-configured"]}),
            ("invalid", {"provider": "template", "meal_style": "not-configured"}),
            ("invalid", {"provider": "template", "retailer_ids": ["not-configured"]}),
            ("invalid", {"servings": 100}),
            ("", {}),
            ("n" * 101, {}),
            ("transient", {"pantry": {}}),
            ("transient", {"use_first": []}),
            ("transient", {"request_id": str(uuid4())}),
        ):
            response = await client.http.post(
                "/v1/me/presets",
                json={
                    "name": name,
                    "parameters": params,
                },
            )
            assert response.status_code == (409 if name == "Večeře" else 422), response.text
        assert (await client.presets()).total == 1
        changed = await client.update_preset(
            saved.id,
            PresetUpdate(
                expected_revision=1,
                name="Dinner",
                parameters={"provider": "template", "servings": 4},
            ),
        )
        assert changed.name == "Dinner" and changed.revision == 2
        assert changed.parameters["servings"] == 4
        with pytest.raises(BackendError) as conflict:
            await client.update_preset(saved.id, PresetUpdate(expected_revision=1, name="Old"))
        assert conflict.value.status == 409
        with pytest.raises(BackendError) as conflict:
            await client.delete_preset(saved.id, expected_revision=1)
        assert conflict.value.status == 409
        async with AsyncGroceryClient("http://test", other_token, transport=transport) as other:
            assert (await other.presets()).total == 0
            for method, path, kwargs in (
                ("GET", f"/v1/me/presets/{saved.id}", {}),
                (
                    "PATCH",
                    f"/v1/me/presets/{saved.id}",
                    {
                        "json": {
                            "name": "Stolen",
                            "expected_revision": 2,
                        }
                    },
                ),
                ("DELETE", f"/v1/me/presets/{saved.id}", {"params": {"expected_revision": 2}}),
                ("GET", "/v1/me/presets/missing", {}),
            ):
                assert (await other.http.request(method, path, **kwargs)).status_code == 404
            # Identical names in separate accounts are permitted.
            assert (
                await other.create_preset(
                    PresetCreate(
                        name="Dinner",
                        parameters={"provider": "template"},
                    )
                )
            ).revision == 1
        await client.delete_preset(saved.id, expected_revision=2)
        assert (await client.presets()).total == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parameters",
    [
        {"provider": "ollama", "model": "not-configured"},
        {"servings": 100},
        {"unknown_selection": True},
    ],
)
async def test_preset_patch_owner_and_revision_precede_semantic_validation(
    accounts: Any,
    parameters: dict[str, Any],
) -> None:
    _, _, app, _, _, token, other_token = accounts
    async with AsyncGroceryClient(
        "http://test", token, transport=httpx.ASGITransport(app)
    ) as client:
        saved = await client.create_preset(
            PresetCreate(
                name="Unchanged",
                parameters={"provider": "template"},
            )
        )
        path = f"/v1/me/presets/{saved.id}"
        body = {"expected_revision": 0, "parameters": parameters}
        assert (await client.http.patch(path, json=body)).status_code == 409
        assert (
            await client.http.patch(
                path,
                json=body,
                headers={
                    "Authorization": f"Bearer {other_token}",
                },
            )
        ).status_code == 404
        assert (await client.http.patch("/v1/me/presets/missing", json=body)).status_code == 404
        body["expected_revision"] = 1
        assert (await client.http.patch(path, json=body)).status_code == 422
        assert (await client.preset(saved.id)) == saved


@pytest.mark.asyncio
async def test_preset_patch_write_during_validation_still_conflicts(accounts: Any) -> None:
    control, repo, _, user, _, token, _ = accounts
    saved = repo.create_preset(user.user_id, "Original", {"provider": "template"}, NOW)

    def validate(request: RecipeRequest) -> RecipeRequest:
        repo.update_preset(
            user.user_id,
            saved.id,
            PresetUpdate(expected_revision=1, name="Concurrent"),
            None,
            NOW,
        )
        return request

    app = FastAPI()
    mount_user_state(app, control, lambda: user, lambda: NOW, validate)
    async with AsyncGroceryClient(
        "http://test", token, transport=httpx.ASGITransport(app)
    ) as client:
        with pytest.raises(BackendError) as conflict:
            await client.update_preset(
                saved.id,
                PresetUpdate(
                    expected_revision=1,
                    name="Overwrite",
                    parameters={"provider": "template"},
                ),
            )
        assert conflict.value.status == 409
    current = repo.preset(user.user_id, saved.id)
    assert current.name == "Concurrent" and current.revision == 2
    assert current.parameters == saved.parameters


@pytest.mark.asyncio
async def test_legacy_profile_parameters_are_visible_stale_and_never_rewritten(
    accounts: Any,
) -> None:
    control, _, app, user, _, token, _ = accounts
    legacy_id = str(uuid4())
    legacy = {"pantry": {"rice": "500g"}, "legacy_selection": "unknown"}
    with control.sessions.begin() as session:
        session.add(
            ProfileRow(
                id=legacy_id,
                user_id=user.user_id,
                name="Dinner",
                revision=7,
                parameters=legacy,
                created_at=NOW,
                updated_at=NOW,
            )
        )
    async with AsyncGroceryClient(
        "http://test", token, transport=httpx.ASGITransport(app)
    ) as client:
        saved = await client.preset(legacy_id)
        assert saved.revision == 7 and saved.parameters == legacy and saved.stale
        assert (await client.presets()).items[0] == saved
        renamed = await client.update_preset(
            legacy_id,
            PresetUpdate(
                expected_revision=7,
                name="Old Dinner",
            ),
        )
        assert renamed.parameters == legacy and renamed.stale
        assert (
            await client.http.patch(
                f"/v1/me/presets/{legacy_id}",
                json={
                    "expected_revision": 8,
                    "parameters": {"pantry": {}},
                },
            )
        ).status_code == 422
    with control.sessions() as session:
        row = session.get(ProfileRow, legacy_id)
        assert row and row.parameters == legacy and row.revision == 8


@pytest.mark.asyncio
async def test_job_request_restoration_is_owned_and_recipe_only(accounts: Any) -> None:
    control, _, app, user, _, token, other_token = accounts
    request = RecipeRequest(
        provider="template",
        language="cs",
        pantry={"rice": "150g"},
        use_first=("rice",),
        max_cost_per_serving_czk="12.34",
    )
    job = control.submit(user.user_id, request.model_dump(mode="json"), "recipe", NOW)
    refresh = control.submit(user.user_id, {"source_id": "kupi"}, "refresh", NOW, kind="refresh")
    transport = httpx.ASGITransport(app)
    async with AsyncGroceryClient("http://test", token, transport=transport) as client:
        assert (await client.job_request(job.job_id)) == request
        for job_id in (refresh.job_id, "missing"):
            with pytest.raises(BackendError) as missing:
                await client.job_request(job_id)
            assert missing.value.status == 404
    async with AsyncGroceryClient("http://test", other_token, transport=transport) as other:
        with pytest.raises(BackendError) as missing:
            await other.job_request(job.job_id)
        assert missing.value.status == 404


def attempt(action: Any) -> int:
    try:
        action()
        return 200
    except HTTPException as exc:
        return exc.status_code


def test_concurrent_initial_writes_and_revisions_have_one_winner(accounts: Any) -> None:
    _, repo, _, user, _, _, _ = accounts
    for action in (
        lambda: repo.patch_settings(
            user.user_id, SettingsPatch(expected_revision=0, ui_language="cs")
        ),
        lambda: repo.put_pantry(user.user_id, PantryPut(expected_revision=0, items={})),
    ):
        with ThreadPoolExecutor(max_workers=4) as pool:
            statuses = list(pool.map(lambda _, operation=action: attempt(operation), range(4)))
        assert sorted(statuses) == [200, 409, 409, 409]
    preset = repo.create_preset(user.user_id, "Concurrent", {"provider": "template"}, NOW)
    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(
            pool.map(
                lambda _: attempt(
                    lambda: repo.update_preset(
                        user.user_id,
                        preset.id,
                        PresetUpdate(expected_revision=1, name="Changed"),
                        None,
                        NOW,
                    )
                ),
                range(4),
            )
        )
    assert sorted(statuses) == [200, 409, 409, 409]


def test_concurrent_preset_creates_enforce_quota_and_unique_names(accounts: Any) -> None:
    control, repo, _, user, other, _, _ = accounts
    with control.sessions.begin() as session:
        session.add_all(
            [
                ProfileRow(
                    id=str(uuid4()),
                    user_id=user.user_id,
                    name=f"Preset {n}",
                    revision=1,
                    parameters={"provider": "template"},
                    created_at=NOW,
                    updated_at=NOW,
                )
                for n in range(99)
            ]
        )
    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(
            pool.map(
                lambda n: attempt(
                    lambda: repo.create_preset(
                        user.user_id,
                        f"Racing {n}",
                        {"provider": "template"},
                        NOW,
                    )
                ),
                range(4),
            )
        )
    assert sorted(statuses) == [200, 409, 409, 409]
    assert repo.presets(user.user_id).total == 100
    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(
            pool.map(
                lambda _: attempt(
                    lambda: repo.create_preset(
                        other.user_id,
                        "Same name",
                        {"provider": "template"},
                        NOW,
                    )
                ),
                range(4),
            )
        )
    assert sorted(statuses) == [200, 409, 409, 409]
    assert repo.presets(other.user_id).total == 1


@pytest.mark.asyncio
async def test_mount_uses_injected_validator_and_flags_changed_selections(accounts: Any) -> None:
    control, _, _, user, _, token, _ = accounts
    permitted = True
    seen: list[RecipeRequest] = []

    def validate(request: RecipeRequest) -> RecipeRequest:
        seen.append(request)
        if not permitted:
            raise HTTPException(422, "Selection disabled")
        return request.model_copy(update={"cache_policy": "cache-only"})

    app = FastAPI()
    mount_user_state(app, control, lambda: user, lambda: NOW, validate)
    async with AsyncGroceryClient(
        "http://test", token, transport=httpx.ASGITransport(app)
    ) as client:
        saved = await client.create_preset(PresetCreate(name="Validated", parameters={}))
        assert seen and saved.parameters["cache_policy"] == "cache-only"
        permitted = False
        assert (await client.presets()).items[0].stale
        with pytest.raises(BackendError) as invalid:
            await client.update_preset(saved.id, PresetUpdate(expected_revision=1, parameters={}))
        assert invalid.value.status == 422
        assert (await client.preset(saved.id)).revision == 1


@pytest.mark.asyncio
async def test_account_cookie_csrf_and_no_store(accounts: Any, tmp_path: Path) -> None:
    control, repo, _, user, _, _, _ = accounts
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'csrf-offers.db'}",
            meal_config=ROOT / "config/meals.toml",
            _env_file=None,
        ),
        BackendSettings(
            cookie_sessions_enabled=True,
            trusted_origin="http://localhost",
            cookie_secure=False,
            _env_file=None,
        ),
        engine=control.sessions.kw["bind"],
        clock=lambda: NOW,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://localhost",
    ) as client:
        accepted = await client.post(
            "/v1/invitations/accept",
            json={
                "token": control.invite("cookie-account", NOW),
                "session_mode": "cookie",
            },
        )
        assert accepted.status_code == 201
        csrf = accepted.json()["csrf_token"]
        good_headers = {"Origin": "http://localhost", "X-CSRF-Token": csrf}
        for method, path, body in (
            ("PATCH", "/v1/me/settings", {"expected_revision": 0, "ui_language": "cs"}),
            ("PUT", "/v1/me/pantry", {"expected_revision": 0, "items": {}}),
            ("POST", "/v1/me/presets", {"name": "Dinner", "parameters": {"provider": "template"}}),
        ):
            denied = await client.request(method, path, json=body)
            assert denied.status_code == 403
            assert denied.headers["Cache-Control"] == "no-store"
            denied = await client.request(
                method,
                path,
                json=body,
                headers={
                    **good_headers,
                    "Origin": "https://untrusted.example",
                },
            )
            assert denied.status_code == 403
            saved = await client.request(method, path, json=body, headers=good_headers)
            assert saved.status_code in {200, 201}, saved.text
            assert saved.headers["Cache-Control"] == "no-store"
        presets = await client.get("/v1/me/presets")
        assert presets.headers["Cache-Control"] == "no-store"
        preset_id = presets.json()["items"][0]["id"]
        path = f"/v1/me/presets/{preset_id}"
        assert (
            await client.patch(
                path,
                json={
                    "name": "Lunch",
                    "expected_revision": 1,
                },
            )
        ).status_code == 403
        assert (await client.delete(path, params={"expected_revision": 1})).status_code == 403
        assert (
            await client.delete(
                path,
                params={"expected_revision": 1},
                headers=good_headers,
            )
        ).status_code == 204
    assert repo.settings(user.user_id)["revision"] == 0


def test_sync_client_account_paths_revision_payloads_and_decimal_serialization() -> None:
    seen: list[httpx.Request] = []
    preset = {"id": "preset", "name": "Dinner", "revision": 1, "parameters": {}}

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["Authorization"] == "Bearer secret"
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        if path == "/v1/me/settings":
            if body:
                assert body == {"expected_revision": 0, "ui_language": None}
            return httpx.Response(
                200, json={"revision": 0, "ui_language": None, "recipe_language": None}
            )
        if path == "/v1/me/pantry":
            if body:
                assert body == {
                    "expected_revision": 0,
                    "items": {
                        "rice": {"grams": "1.5", "use_first": True},
                    },
                }
            return httpx.Response(200, json={"revision": 0, "items": {}})
        if path == "/v1/me/presets":
            return httpx.Response(
                200, json={"items": [preset], "total": 1} if request.method == "GET" else preset
            )
        if path == "/v1/me/presets/preset":
            if request.method == "DELETE":
                assert request.url.params["expected_revision"] == "1" and not request.content
                return httpx.Response(204)
            return httpx.Response(200, json=preset)
        if path == "/v1/jobs/job/request":
            return httpx.Response(200, json=RecipeRequest(language="cs").model_dump(mode="json"))
        return httpx.Response(404, json={"detail": "Missing"})

    with GroceryClient("http://test", "secret", transport=httpx.MockTransport(handle)) as client:
        assert client.settings().ui_language is None
        client.patch_settings(SettingsPatch(expected_revision=0, ui_language=None))
        client.pantry()
        client.put_pantry(
            PantryPut(
                expected_revision=0,
                items={
                    "rice": PantryItem(grams="01.500", use_first=True),
                },
            )
        )
        assert client.presets().total == 1
        assert client.create_preset(PresetCreate(name="Dinner", parameters={})).id == "preset"
        client.preset("preset")
        client.update_preset("preset", PresetUpdate(expected_revision=1, name="Renamed"))
        client.delete_preset("preset", expected_revision=1)
        assert client.job_request("job").language == "cs"
        with pytest.raises(BackendError) as missing:
            client.preset("missing")
        assert missing.value.status == 404
    assert len(seen) == 11
