"""Reusable workflows with injected acquisition and offer-read boundaries."""

import hashlib
import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

import httpx

from grocery_agent.application.commands import MealCommand, WorkflowCommand
from grocery_agent.application.parameters import MealParameters, resolve_parameters
from grocery_agent.catalogue.base import CataloguePublisher, ProfileCataloguePublisher
from grocery_agent.catalogue.profiles import AcquisitionProfile, Coverage
from grocery_agent.meals.catalog import MealCatalog
from grocery_agent.meals.planner import MealReport, plan_meals
from grocery_agent.models.common import DomainModel, utc_now
from grocery_agent.persistence.reader import BatchOfferReader
from grocery_agent.pipeline.results import ScrapeResult
from grocery_agent.pipeline.service import ScrapePipeline
from grocery_agent.stores.base import AcquisitionAdapter, AdapterContext

logger = logging.getLogger(__name__)


class AcquisitionService:
    def __init__(
        self,
        pipeline: ScrapePipeline,
        http: httpx.AsyncClient,
        catalogue: CataloguePublisher | None = None,
    ) -> None:
        self.pipeline = pipeline
        self.http = http
        self.catalogue = catalogue

    async def acquire(
        self,
        adapter: AcquisitionAdapter,
        run_id: UUID | None = None,
        profile: AcquisitionProfile | None = None,
    ) -> ScrapeResult:
        if profile is not None:
            if not isinstance(self.catalogue, ProfileCataloguePublisher):
                raise ValueError("profile acquisition requires a profile-aware catalogue publisher")
            from grocery_agent.acquisition.profiles import profile_adapter

            adapter, profile = profile_adapter(adapter, profile)
        result = await self.pipeline.run(
            adapter, AdapterContext(self.http, str(run_id) if run_id is not None else None, profile)
        )
        if profile is not None:
            assert isinstance(self.catalogue, ProfileCataloguePublisher)
            self.catalogue.record_profile(
                result.run_id,
                profile,
                Coverage(
                    profile_fingerprint=profile.fingerprint,
                    observed=result.accepted,
                    complete=result.status == "success"
                    and result.accepted > 0
                    and profile.coverage == "complete",
                ),
            )
        if result.status == "success" and self.catalogue is not None:
            self.catalogue.publish(result.run_id)
        return result

    async def acquire_many(
        self,
        adapters: Sequence[AcquisitionAdapter],
        *,
        profiles: Sequence[AcquisitionProfile] | None = None,
    ) -> tuple[ScrapeResult, ...]:
        if profiles is not None and len(profiles) != len(adapters):
            raise ValueError("acquisition profiles must align with adapters")
        results = []
        for index, adapter in enumerate(adapters):
            results.append(
                await self.acquire(
                    adapter, profile=profiles[index] if profiles is not None else None
                )
            )
        return tuple(results)


class MealExecution(DomainModel):
    version: Literal[1] = 1
    request_id: UUID
    catalog_sha256: str
    parameters: MealParameters
    catalog_snapshot: MealCatalog
    report: MealReport


class MealService:
    def __init__(
        self,
        reader: BatchOfferReader,
        catalog: MealCatalog,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.reader = reader
        self._catalog = MealCatalog.model_validate(catalog.model_dump())
        payload = json.dumps(
            self._catalog.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        self.catalog_sha256 = hashlib.sha256(payload.encode()).hexdigest()
        self.clock = clock

    def parameters(
        self, command: MealCommand | WorkflowCommand, profile: MealParameters | None = None
    ) -> MealParameters:
        return resolve_parameters(self._catalog, command.overrides, profile)

    def plan(self, command: MealCommand, profile: MealParameters | None = None) -> MealExecution:
        parameters = self.parameters(command, profile)
        catalog = parameters.apply_to(self._catalog)
        batch = (
            self.reader.get_batch(str(command.run_id), parameters.policy.source_id)
            if command.run_id is not None
            else self.reader.latest_batch(parameters.policy.source_id)
        )
        report = plan_meals(self.reader, catalog, self.clock(), batch=batch)
        execution = MealExecution(
            request_id=command.request_id,
            catalog_sha256=self.catalog_sha256,
            parameters=parameters,
            catalog_snapshot=catalog,
            report=report,
        )
        logger.info(
            "meal_report_finished",
            extra={
                "fields": {
                    "request_id": str(command.request_id),
                    "run_id": report.run_id,
                    "meals": len(report.meals),
                    "location": report.policy.location_label,
                    "catalog_sha256": self.catalog_sha256,
                }
            },
        )
        return execution


@dataclass(frozen=True)
class WorkflowExecution:
    acquisition: ScrapeResult
    meal: MealExecution


class AcquisitionFailed(ValueError):
    def __init__(self, result: ScrapeResult) -> None:
        self.result = result
        super().__init__(
            f"acquisition {result.run_id} ended as {result.status}; no new meal report"
        )


class WorkflowService:
    def __init__(self, acquisition: AcquisitionService, meals: MealService) -> None:
        self.acquisition = acquisition
        self.meals = meals

    async def run(
        self,
        command: WorkflowCommand,
        adapter: AcquisitionAdapter,
        profile: MealParameters | None = None,
    ) -> WorkflowExecution:
        parameters = self.meals.parameters(command, profile)
        if adapter.source_id != parameters.policy.source_id:
            raise ValueError("acquisition source does not match the meal request")
        result = await self.acquisition.acquire(adapter)
        if result.status != "success":
            raise AcquisitionFailed(result)
        meal = self.meals.plan(
            MealCommand(
                request_id=command.request_id,
                overrides=command.overrides,
                run_id=UUID(result.run_id),
            ),
            profile,
        )
        return WorkflowExecution(result, meal)
