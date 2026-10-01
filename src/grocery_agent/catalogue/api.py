"""Optional private, read-only HTTP interface for the published catalogue."""

import logging
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime
from typing import Annotated, Literal

from fastapi import FastAPI, HTTPException, Query
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from grocery_agent.catalogue.config import CatalogueSettings
from grocery_agent.catalogue.models import CataloguePage, CatalogueQuery, CatalogueState
from grocery_agent.catalogue.repository import SQLAlchemyCatalogueRepository
from grocery_agent.catalogue.schema import CatalogueHeadRow
from grocery_agent.config import Settings
from grocery_agent.models.common import StoreId, utc_now
from grocery_agent.models.product import Unit
from grocery_agent.persistence.database import create_database_engine
from grocery_agent.persistence.migrations import require_offer_schema

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    catalogue_settings: CatalogueSettings | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> FastAPI:
    settings = settings or Settings()
    catalogue_settings = catalogue_settings or CatalogueSettings()
    # A lazy mode=ro engine can start before the collector creates its database.
    engine = create_database_engine(settings.database_url, read_only=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            engine.dispose()

    app = FastAPI(title="Grocery catalogue", version="1", lifespan=lifespan)
    repository = SQLAlchemyCatalogueRepository(engine, catalogue_settings)

    @contextmanager
    def available_catalogue() -> Iterator[None]:
        try:
            require_offer_schema(engine)
            yield
        except (ValueError, SQLAlchemyError) as exc:
            logger.warning(
                "catalogue_unavailable", extra={"fields": {"error_type": type(exc).__name__}}
            )
            # Storage details and connection URLs stay out of HTTP responses.
            raise HTTPException(503, "Catalogue unavailable or stale; collection required") from exc

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready(source: StoreId = "kupi") -> dict[str, str]:
        with available_catalogue():
            repository.state(source, clock())
        return {"status": "ready", "source": source}

    @app.get("/v1/sources")
    def sources() -> tuple[str, ...]:
        with available_catalogue(), engine.connect() as connection:
            return tuple(
                connection.scalars(
                    select(CatalogueHeadRow.source_id).order_by(CatalogueHeadRow.source_id)
                )
            )

    @app.get("/v1/status/{source}", response_model=CatalogueState)
    def status(source: StoreId) -> CatalogueState:
        with available_catalogue():
            return repository.state(source, clock())

    @app.get("/v1/offers", response_model=CataloguePage)
    def offers(
        source: StoreId,
        scope: str | None = None,
        category: str | None = None,
        retailer: Annotated[list[str] | None, Query()] = None,
        search: Annotated[str | None, Query(max_length=200)] = None,
        unit: Unit | None = None,
        max_unit_price: str | None = None,
        allow_loyalty: bool = False,
        include_from: bool = False,
        include_unavailable: bool = False,
        sort: Literal["name", "unit_price"] = "name",
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> CataloguePage:
        try:
            query = CatalogueQuery.model_validate(
                {
                    "source_id": source,
                    "scope": scope,
                    "category": category,
                    "retailers": tuple(retailer or ()),
                    "search": search,
                    "unit": unit,
                    "max_unit_price": max_unit_price,
                    "allow_loyalty": allow_loyalty,
                    "include_from": include_from,
                    "include_unavailable": include_unavailable,
                    "sort": sort,
                    "offset": offset,
                    "limit": limit,
                }
            )
        except ValidationError as exc:
            raise HTTPException(422, "Invalid catalogue filters; check units and price") from exc
        with available_catalogue():
            return repository.search(query, clock())

    return app
