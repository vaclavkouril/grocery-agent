from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import Engine

from grocery_agent.catalogue.api import create_app
from grocery_agent.catalogue.repository import SQLAlchemyCatalogueRepository
from grocery_agent.config import Settings
from grocery_agent.models.offer import Offer
from grocery_agent.persistence.repository import SQLAlchemyOfferRepository
from grocery_agent.persistence.snapshots import FileSnapshotStore
from tests.application_support import NOW, complete_batch, failed_batch


@asynccontextmanager
async def local_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://catalogue.test"
        ) as client:
            yield client


@pytest.mark.asyncio
async def test_catalogue_api_reports_failure_without_exposing_expired_data(
    engine: Engine,
    repository: SQLAlchemyOfferRepository,
    snapshots: FileSnapshotStore,
    candidate: dict[str, Any],
) -> None:
    run = complete_batch(repository, snapshots, [Offer.model_validate(candidate)])
    SQLAlchemyCatalogueRepository(engine).publish(run.run_id)
    failed_batch(repository, NOW + timedelta(minutes=1))
    settings = Settings(database_url=str(engine.url), _env_file=None)
    app = create_app(settings, clock=lambda: NOW + timedelta(minutes=1))
    async with local_client(app) as client:
        assert (await client.get("/health/live")).status_code == 200
        assert (await client.get("/health/ready?source=mock")).status_code == 200
        assert (await client.get("/v1/sources")).json() == ["mock"]
        state = (await client.get("/v1/status/mock")).json()
        assert state["degraded"] and state["latest_run_status"] == "failed"
        page = (await client.get("/v1/offers?source=mock&unit=l&sort=unit_price&limit=1")).json()
        assert page["total"] == 1 and page["state"]["warnings"]
        assert page["items"][0]["offer"]["current_price"] == "19.9"
        assert (await client.get("/v1/offers?source=mock&sort=unit_price")).status_code == 422
        assert (await client.get("/v1/offers?source=mock&limit=101")).status_code == 422
        assert (
            await client.get("/v1/offers?source=mock&max_unit_price=-1&unit=l")
        ).status_code == 422
        assert (await client.post("/v1/offers?source=mock")).status_code == 405
    stale = create_app(settings, clock=lambda: NOW + timedelta(hours=37))
    async with local_client(stale) as client:
        assert (await client.get("/v1/offers?source=mock")).status_code == 503


@pytest.mark.asyncio
async def test_api_can_start_before_storage_without_creating_it(tmp_path: Path) -> None:
    path = tmp_path / "missing.db"
    settings = Settings(database_url=f"sqlite:///{path}", _env_file=None)
    async with local_client(create_app(settings)) as client:
        assert (await client.get("/health/live")).status_code == 200
        response = await client.get("/v1/offers?source=mock")
        assert response.status_code == 503
        assert str(path) not in response.text and "sqlite" not in response.text
    assert not path.exists() and not (tmp_path / "control.db").exists()
