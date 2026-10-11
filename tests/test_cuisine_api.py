"""Cuisine choices survive owned preset and job boundaries without model calls."""

from pathlib import Path
from typing import Any

import httpx
import pytest

from grocery_agent.backend.api import create_app
from grocery_agent.backend.config import BackendSettings
from grocery_agent.config import Settings
from grocery_agent.cuisines import CUISINE_IDS, CUISINE_LABELS
from tests.application_support import NOW
from tests.test_account_state import ROOT
from tests.test_account_state import accounts as accounts


@pytest.mark.asyncio
async def test_every_cuisine_is_discoverable_and_can_be_saved(accounts: Any) -> None:
    _, _, app, _, _, token, other_token = accounts
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        caps = (await client.get("/v1/capabilities")).json()
        assert caps["cuisine_ids"] == list(CUISINE_IDS)
        assert caps["cuisine_labels"] == CUISINE_LABELS
        assert caps["defaults"]["cuisine"] is None
        for cuisine in CUISINE_IDS:
            response = await client.post(
                "/v1/me/presets",
                json={
                    "name": CUISINE_LABELS[cuisine]["en"],
                    "parameters": {"provider": "codex", "cuisine": cuisine, "recipe_count": 3},
                },
            )
            assert response.status_code == 201, response.text
            preset = response.json()
            assert preset["parameters"]["cuisine"] == cuisine
            assert (
                not {"pantry", "condiments", "use_first", "request_id"}
                & preset["parameters"].keys()
            )
        listed = (await client.get("/v1/me/presets")).json()
        assert listed["total"] == len(CUISINE_IDS)
        assert all(not item["stale"] for item in listed["items"])
        selected = listed["items"][0]
        changed = await client.patch(
            f"/v1/me/presets/{selected['id']}",
            json={
                "expected_revision": selected["revision"],
                "parameters": {"provider": "codex", "cuisine": "indian"},
            },
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["parameters"]["cuisine"] == "indian"
        foreign = await client.get(
            f"/v1/me/presets/{selected['id']}", headers={"Authorization": f"Bearer {other_token}"}
        )
        assert foreign.status_code == 404
        for cuisine in ("not-configured", "Chinese", "chinnesse", ["greek"], ""):
            invalid = await client.post(
                "/v1/me/presets",
                json={
                    "name": "Invalid",
                    "parameters": {"provider": "codex", "cuisine": cuisine},
                },
            )
            assert invalid.status_code == 422
        submitted = await client.post("/v1/recipes", json=changed.json()["parameters"])
        assert submitted.status_code == 202, submitted.text
        job_id = submitted.json()["job_id"]
        original = (await client.get(f"/v1/jobs/{job_id}/request")).json()
        assert original["cuisine"] == "indian"
        await client.patch(
            f"/v1/me/presets/{selected['id']}",
            json={
                "expected_revision": changed.json()["revision"],
                "parameters": {"provider": "codex", "cuisine": "mexican"},
            },
        )
        assert (await client.get(f"/v1/jobs/{job_id}/request")).json() == original


@pytest.mark.asyncio
async def test_templates_reject_cuisine_before_queueing(accounts: Any, tmp_path: Path) -> None:
    control, _, _, _, _, token, _ = accounts
    app = create_app(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'absent.db'}",
            meal_config=ROOT / "config/meals.toml",
            _env_file=None,
        ),
        BackendSettings(providers=("template",), _env_file=None),
        engine=control.sessions.kw["bind"],
        clock=lambda: NOW,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        for path, payload in (
            ("/v1/recipes", {"provider": "template", "cuisine": "greek"}),
            (
                "/v1/me/presets",
                {"name": "Greek", "parameters": {"provider": "template", "cuisine": "greek"}},
            ),
        ):
            response = await client.post(path, json=payload)
            assert response.status_code == 422
            assert "cuisine" in response.json()["detail"].lower()
        assert (await client.get("/v1/jobs")).json()["total"] == 0
        assert (await client.get("/v1/me/presets")).json()["total"] == 0
        assert (await client.post("/v1/recipes", json={"provider": "template"})).status_code == 202
