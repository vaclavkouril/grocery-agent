import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic import command
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
from sqlalchemy import text

from grocery_agent.account_models import PantryItem, PantryPut, PresetCreate, SettingsPatch
from grocery_agent.backend.api import create_app
from grocery_agent.backend.config import BackendSettings
from grocery_agent.backend.user_state import mount_user_state
from grocery_agent.config import Settings
from grocery_agent.http_client import AsyncGroceryClient, GroceryClient
from grocery_agent.persistence.control.schema import JobRow, ProfileRow
from grocery_agent.persistence.database import create_database_engine
from grocery_agent.persistence.migrations import migration_config, upgrade_database
from grocery_agent.recipes import RecipeRequest
from tests.application_support import NOW
from tests.test_account_state import ROOT
from tests.test_account_state import accounts as accounts
from tests.test_saved_recipes import finished_job


@pytest.mark.asyncio
async def test_condiments_omission_clear_conflict_and_private_persistence(accounts: Any) -> None:
    _, repo, app, user, _, token, other_token = accounts
    async with AsyncGroceryClient(
        "http://test", token, transport=httpx.ASGITransport(app)
    ) as client:
        assert (await client.pantry()).condiments == ()
        saved = await client.put_pantry(
            PantryPut(
                expected_revision=0,
                items={"rice": PantryItem(grams="1.5")},
                condiments=("salt", "black_pepper"),
            )
        )
        assert saved.condiments == ("salt", "black_pepper")
        # The typed client must omit the unset default, preserving old callers.
        saved = await client.put_pantry(PantryPut(expected_revision=1, items={}))
        assert saved.condiments == ("salt", "black_pepper")
        saved = await client.http.put(
            "/v1/me/pantry",
            json={
                "expected_revision": 2,
                "items": {"rice": {"grams": None}},
            },
        )
        assert saved.json()["condiments"] == ["salt", "black_pepper"]
        assert repo.pantry(user.user_id).condiments == ("salt", "black_pepper")
        assert (
            await client.http.put(
                "/v1/me/pantry",
                json={
                    "expected_revision": 2,
                    "items": {},
                    "condiments": [],
                },
            )
        ).status_code == 409
        saved = await client.put_pantry(PantryPut(expected_revision=3, items={}, condiments=()))
        assert saved.condiments == ()
        for condiments in (["sugar"], ["salt", "salt"], None):
            assert (
                await client.http.put(
                    "/v1/me/pantry",
                    json={
                        "expected_revision": 4,
                        "items": {},
                        "condiments": condiments,
                    },
                )
            ).status_code == 422
    async with AsyncGroceryClient(
        "http://test", other_token, transport=httpx.ASGITransport(app)
    ) as other:
        assert (await other.pantry()).revision == 0


@pytest.mark.parametrize("value", [True, False, 0, 4, "2", 2.0])
def test_preferred_max_stores_is_strict(value: Any) -> None:
    with pytest.raises(ValidationError):
        SettingsPatch(expected_revision=0, max_stores=value)


@pytest.mark.asyncio
async def test_preferences_defaults_partial_patch_null_and_allowed_retailers(accounts: Any) -> None:
    _, repo, app, user, _, token, other_token = accounts
    assert repo.settings(user.user_id)["retailer_ids"] is None
    assert repo.settings(user.user_id)["max_stores"] is None
    async with AsyncGroceryClient(
        "http://test", token, transport=httpx.ASGITransport(app)
    ) as client:
        saved = await client.patch_settings(
            SettingsPatch(
                expected_revision=0,
                retailer_ids=("lidl", "albert"),
                max_stores=2,
            )
        )
        assert saved.retailer_ids == ("lidl", "albert") and saved.max_stores == 2
        saved = await client.patch_settings(SettingsPatch(expected_revision=1, ui_language="cs"))
        assert saved.retailer_ids == ("lidl", "albert") and saved.max_stores == 2
        for retailers in (["not-configured"], ["lidl", "lidl"], ["INVALID"]):
            rejected = await client.http.patch(
                "/v1/me/settings",
                json={
                    "expected_revision": 2,
                    "retailer_ids": retailers,
                },
            )
            assert rejected.status_code == 422
        assert (await client.settings()).revision == 2
        saved = await client.patch_settings(
            SettingsPatch(
                expected_revision=2,
                retailer_ids=None,
                max_stores=None,
            )
        )
        assert saved.retailer_ids is None and saved.max_stores is None
        assert saved.ui_language == "cs"
    async with AsyncGroceryClient(
        "http://test", other_token, transport=httpx.ASGITransport(app)
    ) as other:
        assert (await other.settings()).revision == 0


@pytest.mark.asyncio
async def test_preferences_use_injected_recipe_validator(accounts: Any) -> None:
    control, _, _, user, _, token, _ = accounts
    seen: list[RecipeRequest] = []

    def validate(request: RecipeRequest) -> RecipeRequest:
        seen.append(request)
        raise HTTPException(422, "Retailer disabled")

    app = FastAPI()
    mount_user_state(app, control, lambda: user, lambda: NOW, validate)
    async with AsyncGroceryClient(
        "http://test", token, transport=httpx.ASGITransport(app)
    ) as client:
        response = await client.http.patch(
            "/v1/me/settings",
            json={
                "expected_revision": 0,
                "retailer_ids": ["lidl"],
            },
        )
        assert response.status_code == 422 and seen[0].retailer_ids == ("lidl",)
        assert (await client.settings()).revision == 0


def test_control_0006_backfills_without_changing_account_state(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    config = migration_config("control")
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "control_0005")
        connection.execute(
            text(
                "INSERT INTO users (id, username, role, enabled, created_at) "
                "VALUES ('old', 'old', 'user', 1, :now)"
            ),
            {"now": NOW.isoformat()},
        )
        connection.execute(
            text(
                "INSERT INTO account_state "
                "(user_id, settings_revision, ui_language, recipe_language, "
                "pantry_revision, pantry_items) "
                "VALUES ('old', 7, 'cs', 'en', 9, :items)"
            ),
            {"items": json.dumps({"rice": {"grams": "12.3", "use_first": True}})},
        )
        original = dict(connection.execute(text("SELECT * FROM account_state")).mappings().one())
    assert upgrade_database(engine, "control") == "control_0006"
    assert upgrade_database(engine, "control") == "control_0006"
    with engine.connect() as connection:
        migrated = dict(connection.execute(text("SELECT * FROM account_state")).mappings().one())
    assert {key: migrated[key] for key in original} == original
    assert json.loads(migrated["pantry_condiments"]) == []
    assert migrated["preferred_retailers"] is None
    assert migrated["preferred_max_stores"] is None
    engine.dispose()


@pytest.mark.asyncio
async def test_new_account_writes_require_cookie_csrf(accounts: Any, tmp_path: Path) -> None:
    control, _, _, _, _, _, _ = accounts
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'offers.db'}",
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
        transport=httpx.ASGITransport(app), base_url="http://localhost"
    ) as client:
        accepted = await client.post(
            "/v1/invitations/accept",
            json={
                "token": control.invite("saved-cookie", NOW),
                "session_mode": "cookie",
            },
        )
        assert accepted.status_code == 201
        user = (await client.get("/v1/me")).json()
        job_id = finished_job(control, user["user_id"])
        headers = {"Origin": "http://localhost", "X-CSRF-Token": accepted.json()["csrf_token"]}
        writes = (
            ("POST", "/v1/me/saved-recipes", {"job_id": job_id, "meal_index": 0}),
            ("PUT", "/v1/me/pantry", {"expected_revision": 0, "items": {}, "condiments": ["salt"]}),
            ("PATCH", "/v1/me/settings", {"expected_revision": 0, "retailer_ids": ["lidl"]}),
        )
        saved_id = ""
        for method, path, body in writes:
            assert (await client.request(method, path, json=body)).status_code == 403
            assert (
                await client.request(
                    method,
                    path,
                    json=body,
                    headers={
                        **headers,
                        "Origin": "https://foreign.example",
                    },
                )
            ).status_code == 403
            response = await client.request(method, path, json=body, headers=headers)
            assert response.status_code in (200, 201), response.text
            assert response.headers["Cache-Control"] == "no-store"
            if method == "POST":
                saved_id = response.json()["id"]
        path = f"/v1/me/saved-recipes/{saved_id}"
        assert (await client.delete(path)).status_code == 403
        assert (await client.get(path)).headers["Cache-Control"] == "no-store"
        assert (await client.delete(path, headers=headers)).status_code == 204


def test_sync_pantry_client_preserves_unset_condiments_and_nested_defaults() -> None:
    bodies: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"revision": len(bodies), "items": {}})

    with GroceryClient("http://test", "secret", transport=httpx.MockTransport(handle)) as client:
        client.put_pantry(PantryPut(expected_revision=0, items={"rice": PantryItem(grams=None)}))
        client.put_pantry(PantryPut(expected_revision=1, items={}, condiments=()))
    assert bodies == [
        {"expected_revision": 0, "items": {"rice": {"grams": None, "use_first": False}}},
        {"expected_revision": 1, "items": {}, "condiments": []},
    ]


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize(
    "shopping",
    [{}, {"retailer_ids": ()}, {"max_stores": 1}, {"retailer_ids": ("lidl",), "max_stores": 2}],
)
@pytest.mark.asyncio
async def test_submit_omits_only_unset_shopping_fields(
    async_mode: bool, shopping: dict[str, Any]
) -> None:
    recipe = RecipeRequest(provider="template", **shopping)
    seen: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        expected = recipe.model_dump(mode="json", exclude_unset=True)
        for field in {"retailer_ids", "max_stores"} - shopping.keys():
            expected.pop(field, None)
        assert body == expected
        assert request.headers["Idempotency-Key"] == str(recipe.request_id)
        # The existing cache-policy omission must continue to use server defaults.
        assert "cache_policy" not in body and "request_id" not in body
        return httpx.Response(
            202,
            json={
                "job_id": "job",
                "kind": "recipe",
                "status": "queued",
                "phase": "queued",
                "attempts": 0,
                "created_at": NOW.isoformat(),
                "updated_at": NOW.isoformat(),
                "error_code": None,
            },
        )

    if async_mode:
        async with AsyncGroceryClient(
            "http://test", "secret", transport=httpx.MockTransport(handle)
        ) as client:
            assert (await client.submit(recipe)).job_id == "job"
    else:
        with GroceryClient("http://test", "secret", transport=httpx.MockTransport(handle)) as sync:
            assert sync.submit(recipe).job_id == "job"
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_async_submit_applies_account_defaults_and_preserves_explicit_choices(
    accounts: Any,
) -> None:
    control, repo, app, user, _, token, _ = accounts
    repo.patch_settings(
        user.user_id,
        SettingsPatch(
            expected_revision=0,
            retailer_ids=("lidl",),
            max_stores=2,
        ),
    )
    async with AsyncGroceryClient(
        "http://test", token, transport=httpx.ASGITransport(app)
    ) as client:
        inherited = await client.submit(RecipeRequest(provider="template"))
        explicit = await client.submit(
            RecipeRequest(
                provider="template",
                retailer_ids=(),
                max_stores=1,
            )
        )
    with control.sessions() as session:
        inherited_request = session.get(JobRow, inherited.job_id).request
        explicit_request = session.get(JobRow, explicit.job_id).request
    assert inherited_request["retailer_ids"] == ["lidl"]
    assert inherited_request["max_stores"] == 2
    assert explicit_request["retailer_ids"] == []
    assert explicit_request["max_stores"] == 1


@pytest.mark.asyncio
async def test_presets_exclude_transient_condiments_and_preserve_legacy_rows(accounts: Any) -> None:
    control, repo, app, user, _, token, _ = accounts
    async with AsyncGroceryClient(
        "http://test", token, transport=httpx.ASGITransport(app)
    ) as client:
        saved = await client.create_preset(
            PresetCreate(
                name="Dinner",
                parameters={"provider": "template"},
            )
        )
        assert "condiments" not in saved.parameters
        for condiments in (["salt"], [], None):
            create = await client.http.post(
                "/v1/me/presets",
                json={
                    "name": "Inventory",
                    "parameters": {
                        "provider": "template",
                        "condiments": condiments,
                    },
                },
            )
            assert create.status_code == 422
            patch = await client.http.patch(
                f"/v1/me/presets/{saved.id}",
                json={
                    "expected_revision": 1,
                    "parameters": {
                        "provider": "template",
                        "condiments": condiments,
                    },
                },
            )
            assert patch.status_code == 422
        assert (await client.preset(saved.id)) == saved
        legacy = {**saved.parameters, "condiments": ["salt"]}
        with control.sessions.begin() as session:
            session.get(ProfileRow, saved.id).parameters = legacy
        stale = await client.preset(saved.id)
        assert stale.stale and stale.parameters == legacy
        assert (await client.presets()).items[0].stale
    assert repo.preset(user.user_id, saved.id).parameters == legacy
