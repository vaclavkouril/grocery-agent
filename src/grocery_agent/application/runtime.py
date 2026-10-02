"""Local resource composition. These factories do not require accounts."""

from collections.abc import Callable, Sequence
from datetime import datetime

import httpx
from sqlalchemy import Engine, inspect

from grocery_agent.application.commands import MealCommand, WorkflowCommand
from grocery_agent.application.parameters import resolve_parameters
from grocery_agent.application.services import (
    AcquisitionService,
    MealExecution,
    MealService,
    WorkflowExecution,
    WorkflowService,
)
from grocery_agent.catalogue.profiles import AcquisitionProfile
from grocery_agent.catalogue.repository import PublishedOfferReader, SQLAlchemyCatalogueRepository
from grocery_agent.config import Settings
from grocery_agent.matching.base import ExactGTINResolver
from grocery_agent.meals.catalog import MealCatalog
from grocery_agent.models.common import utc_now
from grocery_agent.persistence.database import create_database_engine, open_database
from grocery_agent.persistence.migrations import require_offer_schema
from grocery_agent.persistence.reader import BatchOfferReader, SQLAlchemyCurrentOfferReader
from grocery_agent.persistence.repository import SQLAlchemyOfferRepository
from grocery_agent.persistence.snapshots import FileSnapshotStore
from grocery_agent.pipeline.results import ScrapeResult
from grocery_agent.pipeline.service import ScrapePipeline
from grocery_agent.stores.base import AcquisitionAdapter


def acquisition_service(
    engine: Engine, http: httpx.AsyncClient, settings: Settings
) -> AcquisitionService:
    pipeline = ScrapePipeline(
        SQLAlchemyOfferRepository(engine),
        FileSnapshotStore(settings.snapshot_dir),
        ExactGTINResolver(),
    )
    return AcquisitionService(pipeline, http, SQLAlchemyCatalogueRepository(engine))


def meal_service(
    engine: Engine,
    catalog: MealCatalog,
    clock: Callable[[], datetime] = utc_now,
    *,
    strict_latest: bool = False,
) -> MealService:
    require_offer_schema(engine)
    reader: BatchOfferReader = SQLAlchemyCurrentOfferReader(engine)
    if not strict_latest and inspect(engine).has_table("catalogue_heads"):
        reader = PublishedOfferReader(reader, SQLAlchemyCatalogueRepository(engine), clock)
    return MealService(reader, catalog, clock)


async def run_acquisition(
    settings: Settings,
    adapters: Sequence[AcquisitionAdapter],
    *,
    profiles: Sequence[AcquisitionProfile] | None = None,
) -> tuple[ScrapeResult, ...]:
    engine = open_database(settings.database_url)
    try:
        async with httpx.AsyncClient(
            timeout=settings.http_timeout_seconds,
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
        ) as http:
            return await acquisition_service(engine, http, settings).acquire_many(
                adapters, profiles=profiles
            )
    finally:
        engine.dispose()


def run_meals(
    settings: Settings,
    catalog: MealCatalog,
    command: MealCommand,
    clock: Callable[[], datetime] = utc_now,
    *,
    strict_latest: bool = False,
) -> MealExecution:
    engine = create_database_engine(settings.database_url, read_only=True)
    try:
        return meal_service(engine, catalog, clock, strict_latest=strict_latest).plan(command)
    finally:
        engine.dispose()


async def run_workflow(
    settings: Settings,
    catalog: MealCatalog,
    command: WorkflowCommand,
    adapter: AcquisitionAdapter,
    clock: Callable[[], datetime] = utc_now,
) -> WorkflowExecution:
    resolve_parameters(catalog, command.overrides)
    engine = open_database(settings.database_url)
    try:
        async with httpx.AsyncClient(
            timeout=settings.http_timeout_seconds,
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
        ) as http:
            service = WorkflowService(
                acquisition_service(engine, http, settings), meal_service(engine, catalog, clock)
            )
            return await service.run(command, adapter)
    finally:
        engine.dispose()
