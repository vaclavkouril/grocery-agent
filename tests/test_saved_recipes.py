import copy
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from grocery_agent.account_models import SavedRecipeCreate
from grocery_agent.http_client import AsyncGroceryClient, GroceryClient
from grocery_agent.persistence.control.schema import JobRow, SavedRecipeRow
from grocery_agent.recipes import RecipeRequest
from tests.application_support import NOW
from tests.test_account_state import accounts as accounts


def finished_job(control: Any, user_id: str, *, meals: int = 2) -> str:
    job = control.submit(
        user_id, RecipeRequest(provider="template").model_dump(mode="json"), str(uuid4()), NOW
    )
    with control.sessions.begin() as session:
        row = session.get(JobRow, job.job_id)
        row.state = "succeeded"
        row.result = {
            "report": {"meals": [{"name": f"Meal {n}"} for n in range(meals)]},
            "legacy_metadata": {"unknown": True},
        }
        row.report_html = "<html>Original recipe history</html>"
    return job.job_id


@pytest.mark.asyncio
async def test_saved_recipe_ownership_dedup_pagination_and_unchanged_history(accounts: Any) -> None:
    control, repo, app, user, _, token, other_token = accounts
    job_id = finished_job(control, user.user_id)
    with control.sessions() as session:
        job = session.get(JobRow, job_id)
        original = copy.deepcopy((job.request, job.result, job.report_html))
    transport = httpx.ASGITransport(app)
    async with AsyncGroceryClient("http://test", token, transport=transport) as client:
        payload = SavedRecipeCreate(job_id=job_id, meal_index=0)
        first = await client.http.post("/v1/me/saved-recipes", json=payload.model_dump())
        assert first.status_code == 201
        repeated = await client.http.post("/v1/me/saved-recipes", json=payload.model_dump())
        assert repeated.status_code == 200 and repeated.json() == first.json()
        saved = await client.save_recipe(payload)
        assert saved.meal == original[1]["report"]["meals"][0]
        assert saved.request == original[0] and saved.result == original[1]
        assert await client.saved_recipe(saved.id) == saved
        await client.save_recipe(SavedRecipeCreate(job_id=job_id, meal_index=1))
        page = await client.saved_recipes(offset=1, limit=1)
        assert page.total == 2 and len(page.items) == 1
        assert (page.offset, page.limit) == (1, 1)
        assert (await client.saved_recipes(offset=2)).items == []
        for params in ({"offset": -1}, {"limit": 0}, {"limit": 101}):
            assert (await client.http.get("/v1/me/saved-recipes", params=params)).status_code == 422
        async with AsyncGroceryClient("http://test", other_token, transport=transport) as other:
            assert (await other.saved_recipes()).total == 0
            for method in ("GET", "DELETE"):
                assert (
                    await other.http.request(method, f"/v1/me/saved-recipes/{saved.id}")
                ).status_code == 404
            for index in (-1, 999):
                assert (
                    await other.http.post(
                        "/v1/me/saved-recipes",
                        json={
                            "job_id": job_id,
                            "meal_index": index,
                        },
                    )
                ).status_code == 404
        await client.delete_saved_recipe(saved.id)
        assert (await client.saved_recipes()).total == 1
    with control.sessions() as session:
        job = session.get(JobRow, job_id)
        assert (job.request, job.result, job.report_html) == original
    assert repo.saved_recipes(user.user_id).total == 1


@pytest.mark.asyncio
async def test_saved_recipe_requires_succeeded_recipe_and_valid_meal(accounts: Any) -> None:
    control, _, app, user, _, token, _ = accounts
    finished = finished_job(control, user.user_id)
    pending = control.submit(user.user_id, {}, "pending", NOW).job_id
    refresh = control.submit(user.user_id, {}, "refresh", NOW, kind="refresh").job_id
    with control.sessions.begin() as session:
        session.get(JobRow, refresh).state = "succeeded"
    async with AsyncGroceryClient(
        "http://test", token, transport=httpx.ASGITransport(app)
    ) as client:
        for job_id, index, status in (
            (finished, -1, 422),
            (finished, 2, 422),
            (pending, 0, 422),
            (refresh, 0, 422),
            ("missing", 0, 404),
        ):
            response = await client.http.post(
                "/v1/me/saved-recipes",
                json={
                    "job_id": job_id,
                    "meal_index": index,
                },
            )
            assert response.status_code == status
        for result in (None, {}, {"report": None}, {"report": {"meals": [None]}}):
            with control.sessions.begin() as session:
                session.get(JobRow, finished).result = result
            response = await client.http.post(
                "/v1/me/saved-recipes",
                json={
                    "job_id": finished,
                    "meal_index": 0,
                },
            )
            assert response.status_code == 422
        assert (await client.saved_recipes()).total == 0


def test_bookmark_concurrency_quota_and_dedup_at_capacity(accounts: Any) -> None:
    control, repo, _, user, other, _, _ = accounts
    job_id = finished_job(control, user.user_id, meals=504)
    with control.sessions.begin() as session:
        session.add_all(
            [
                SavedRecipeRow(
                    id=str(uuid4()),
                    user_id=user.user_id,
                    job_id=job_id,
                    meal_index=n,
                    created_at=NOW,
                )
                for n in range(499)
            ]
        )

    def save(index: int) -> int:
        try:
            _, created = repo.save_recipe(
                user.user_id, SavedRecipeCreate(job_id=job_id, meal_index=index), NOW
            )
            return 201 if created else 200
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sorted(pool.map(save, range(499, 503))) == [201, 409, 409, 409]
    assert save(0) == 200
    assert repo.saved_recipes(user.user_id).total == 500
    other_job = finished_job(control, other.user_id)
    payload = SavedRecipeCreate(job_id=other_job, meal_index=0)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: repo.save_recipe(other.user_id, payload, NOW), range(4)))
    assert sum(created for _, created in results) == 1
    assert len({saved.id for saved, _ in results}) == 1
    with control.sessions() as session:
        assert len(session.scalars(select(SavedRecipeRow)).all()) == 501


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.asyncio
async def test_saved_recipe_typed_clients(async_mode: bool) -> None:
    seen: list[httpx.Request] = []
    saved = {
        "id": "saved",
        "job_id": "job",
        "meal_index": 0,
        "created_at": NOW.isoformat(),
        "meal": {"name": "Dinner"},
        "request": {},
        "result": {},
    }

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["Authorization"] == "Bearer secret"
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.method == "POST":
            assert json.loads(request.content) == {"job_id": "job", "meal_index": 0}
        elif request.url.path == "/v1/me/saved-recipes":
            assert dict(request.url.params) == {"offset": "3", "limit": "7"}
            return httpx.Response(
                200,
                json={
                    "items": [],
                    "total": 1,
                    "offset": 3,
                    "limit": 7,
                },
            )
        return httpx.Response(200, json=saved)

    payload = SavedRecipeCreate(job_id="job", meal_index=0)
    if async_mode:
        async with AsyncGroceryClient(
            "http://test", "secret", transport=httpx.MockTransport(handle)
        ) as client:
            assert (await client.save_recipe(payload)).meal == saved["meal"]
            assert (await client.saved_recipes(offset=3, limit=7)).total == 1
            assert (await client.saved_recipe("saved")).id == "saved"
            await client.delete_saved_recipe("saved")
    else:
        with GroceryClient("http://test", "secret", transport=httpx.MockTransport(handle)) as sync:
            assert sync.save_recipe(payload).meal == saved["meal"]
            assert sync.saved_recipes(offset=3, limit=7).total == 1
            assert sync.saved_recipe("saved").id == "saved"
            sync.delete_saved_recipe("saved")
    assert len(seen) == 4
