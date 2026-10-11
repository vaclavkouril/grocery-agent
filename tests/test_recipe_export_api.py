"""Private recipe exports from persisted history, without external calls."""

import copy
import json
from typing import Any
from uuid import uuid4

import httpx
import pytest

from grocery_agent.http_client import AsyncGroceryClient, BackendError, GroceryClient
from grocery_agent.persistence.control.schema import JobRow
from tests.application_support import NOW
from tests.test_account_state import accounts as accounts


def saved_export_job(control: Any, user_id: str) -> str:
    receipt = control.submit(user_id, {"provider": "template"}, str(uuid4()), NOW)
    meal = {
        "title": "Saved carrot recipe",
        "minutes": 15,
        "servings": 2,
        "lines": [
            {
                "price": {
                    "ingredient_id": "carrot",
                    "label": "Historical carrot",
                    "price_per_kg_czk": "10",
                },
                "required_grams": "200",
                "edible_grams": "122",
                "owned_grams": "200",
                "purchased_grams": "0",
                "usage_cost_czk": "0",
            }
        ],
        "nutrients_per_serving": {
            "kcal": "21.96",
            "protein_g": "0.61",
            "carbs_g": "3.721",
            "fat_g": "0.122",
        },
        "usage_cost_per_serving_czk": "0",
        "protein_g_per_czk": None,
        "use_first_grams": "200",
        "stores": [],
        "steps": ["Trim and cook carrots."],
    }
    second = {**copy.deepcopy(meal), "title": "Second recipe"}
    with control.sessions.begin() as session:
        row = session.get(JobRow, receipt.job_id)
        row.state = "succeeded"
        row.report_html = "<html>Unchanged</html>"
        row.result = {"report": {"meals": [meal, second]}, "private_context": "do-not-export"}
        row.inputs = {
            "catalog": {
                "ingredients": {"carrot": {"labels": {"cs": "Uložená mrkev", "en": "Saved carrot"}}}
            },
            "other_private_input": "do-not-export",
        }
    return receipt.job_id


@pytest.mark.asyncio
async def test_recipe_export_frozen_labels_servings_and_private_headers(accounts: Any) -> None:
    control, _, app, user, _, token, other_token = accounts
    job_id = saved_export_job(control, user.user_id)
    with control.sessions() as session:
        original = copy.deepcopy(session.get(JobRow, job_id).result)
    transport = httpx.ASGITransport(app)
    async with AsyncGroceryClient("http://test", token, transport=transport) as client:
        caps = await client.capabilities()
        assert caps.recipe_export_formats == ("text", "ingredients", "schema-org")
        assert await client.recipe_export(job_id, 0, format="ingredients") == "122 g Uložená mrkev"
        english = await client.recipe_export(job_id, 0, format="ingredients", language="en")
        assert english == "122 g Saved carrot"
        path = f"/v1/jobs/{job_id}/recipes/1/export"
        response = await client.http.get(path, params={"format": "schema-org"})
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Content-Type"].startswith("application/ld+json")
        assert response.headers["Content-Disposition"] == 'attachment; filename="recipe-2.json"'
        data = response.json()
        assert data["@type"] == "Recipe" and data["name"] == "Second recipe"
        assert data["recipeIngredient"] == ["122 g Uložená mrkev"]
        assert data["nutrition"]["proteinContent"] == "0.61 g"
        assert "do-not-export" not in response.text and job_id not in response.text
        assert not {"url", "image", "author"} & data.keys()
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get(path)).status_code == 401
        assert (
            await client.get(path, headers={"Authorization": f"Bearer {other_token}"})
        ).status_code == 404
    with control.sessions() as session:
        assert session.get(JobRow, job_id).result == original


@pytest.mark.asyncio
async def test_export_errors_pending_refresh_empty_and_malformed_history(accounts: Any) -> None:
    control, _, app, user, _, token, _ = accounts
    job_id = saved_export_job(control, user.user_id)
    pending = control.submit(user.user_id, {}, "pending-export", NOW).job_id
    refresh = control.submit(user.user_id, {}, "refresh-export", NOW, kind="refresh").job_id
    with control.sessions.begin() as session:
        session.get(JobRow, refresh).state = "succeeded"
    async with AsyncGroceryClient(
        "http://test", token, transport=httpx.ASGITransport(app)
    ) as client:
        for selected, index, status in (
            (job_id, -1, 422),
            (job_id, 2, 422),
            (pending, 0, 409),
            (refresh, 0, 409),
            ("missing", 0, 404),
        ):
            with pytest.raises(BackendError) as error:
                await client.recipe_export(selected, index)
            assert error.value.status == status
        for params in ({"format": "csv"}, {"language": "de"}):
            assert (
                await client.http.get(f"/v1/jobs/{job_id}/recipes/0/export", params=params)
            ).status_code == 422
        for result in ({}, {"report": {"meals": []}}, {"report": {"meals": [{}]}}):
            with control.sessions.begin() as session:
                session.get(JobRow, job_id).result = result
            with pytest.raises(BackendError) as error:
                await client.recipe_export(job_id, 0)
            assert error.value.status == 422


def test_sync_export_client_quotes_job_paths_and_retains_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.raw_path.startswith(b"/v1/jobs/private%2Fjob/recipes/2/export?")
        assert dict(request.url.params) == {"format": "schema-org", "language": "en"}
        assert request.headers["Authorization"] == "Bearer fixture"
        return httpx.Response(200, content=json.dumps({"name": "Export"}))

    with GroceryClient("http://test", "fixture", transport=httpx.MockTransport(handler)) as client:
        assert json.loads(
            client.recipe_export("private/job", 2, format="schema-org", language="en")
        ) == {"name": "Export"}
