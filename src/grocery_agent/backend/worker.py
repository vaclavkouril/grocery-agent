"""Durable worker with heartbeats, cancellation fencing, and bounded crash recovery."""

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select

from grocery_agent.acquisition.profiles import profiled_adapter
from grocery_agent.application.runtime import acquisition_service
from grocery_agent.catalogue.repository import SQLAlchemyCatalogueRepository
from grocery_agent.catalogue.schema import CatalogueRunProfileRow
from grocery_agent.config import Settings
from grocery_agent.meals.catalog import MealCatalog
from grocery_agent.persistence.database import open_database
from grocery_agent.persistence.schema import ScrapeRunRow
from grocery_agent.recipes import RecipeRequest
from grocery_agent.stores.registry import default_registry
from grocery_agent.workflow import writer_lock

from .config import BackendSettings
from .recipes import RecipeExecutor
from .repository import ClaimedJob, ControlRepository, LeaseLost

logger = logging.getLogger(__name__)


class Worker:
    def __init__(
        self,
        repository: ControlRepository,
        executor: RecipeExecutor,
        settings: Settings,
        backend: BackendSettings,
        clock: Callable[[], datetime],
    ) -> None:
        self.repository, self.executor = repository, executor
        self.settings, self.backend, self.clock = settings, backend, clock

    async def _heartbeat(self, job: ClaimedJob) -> None:
        while True:
            await asyncio.sleep(min(self.backend.heartbeat_seconds, self.backend.lease_seconds / 3))
            alive = await asyncio.to_thread(
                self.repository.heartbeat, job, self.clock(), self.backend.lease_seconds
            )
            if not alive:
                return

    def _refresh(self, job: ClaimedJob, run_id: str) -> tuple[dict[str, object], str]:
        source = job.request["source_id"]
        catalog = MealCatalog.load(self.settings.meal_config)
        scopes = self.backend.allowed_source_scopes(catalog.policy.source_id, catalog.policy.scope)
        if source not in scopes:
            raise ValueError("refresh source is not configured")
        profiles = [
            profile
            for profile in self.backend.acquisition_profiles
            if profile.source_id == source
            and source in scopes
            and (profile.scope is None or profile.scope in scopes[source])
        ]
        profiles = [
            resolved
            for profile in profiles
            for _, resolved in [profiled_adapter(default_registry(), profile)]
            if job.request.get("profile_fingerprint") is None
            or resolved.fingerprint == job.request["profile_fingerprint"]
        ]
        legacy = (
            not self.backend.acquisition_profiles
            and not self.backend.source_scopes
            and source == catalog.policy.source_id
            and job.request.get("profile_fingerprint") is None
        )
        if not legacy and len(profiles) != 1:
            raise ValueError("refresh requires an unambiguous configured source profile")
        profile = None if legacy else profiles[0]
        fingerprint = "legacy" if legacy else profiles[0].fingerprint
        with writer_lock(self.settings.lock_path):
            engine = open_database(self.settings.database_url)
            try:
                with engine.connect() as connection:
                    previous = connection.execute(
                        select(
                            ScrapeRunRow.source_id.label("source_id"), ScrapeRunRow.status
                        ).where(ScrapeRunRow.id == run_id)
                    ).one_or_none()
                    recorded = connection.execute(
                        select(
                            CatalogueRunProfileRow.source_id,
                            CatalogueRunProfileRow.profile_fingerprint,
                        ).where(CatalogueRunProfileRow.run_id == run_id)
                    ).one_or_none()
                if previous is not None and previous.source_id != source:
                    raise ValueError("recorded acquisition belongs to a different source")
                if previous is not None and previous.status == "success":
                    if (recorded is None and not legacy) or (
                        recorded is not None
                        and (
                            recorded.source_id != source
                            or recorded.profile_fingerprint != fingerprint
                        )
                    ):
                        raise ValueError("recorded acquisition profile does not match the request")
                    SQLAlchemyCatalogueRepository(engine).publish(run_id)
                    return {
                        "status": "ok",
                        "run_id": run_id,
                        "recovered": True,
                        "source_id": source,
                        "profile_fingerprint": fingerprint,
                    }, ""
                if previous is not None:
                    # An interrupted acquisition may have retained evidence. Do not replay
                    # it under the same ID or silently initiate another retailer scrape.
                    raise ValueError(
                        "recorded acquisition is incomplete; administrator must resubmit"
                    )

                async def acquire() -> dict[str, object]:
                    async with httpx.AsyncClient(
                        timeout=self.settings.http_timeout_seconds,
                        headers={"User-Agent": self.settings.user_agent},
                        follow_redirects=True,
                    ) as http:
                        result = await acquisition_service(engine, http, self.settings).acquire(
                            default_registry().create(source), UUID(run_id), profile=profile
                        )
                        if result.status != "success":
                            raise ValueError("refresh acquisition did not complete successfully")
                        return {
                            "status": "ok",
                            "run_id": run_id,
                            "source_id": source,
                            "profile_fingerprint": fingerprint,
                            "recovered": False,
                        }

                return asyncio.run(acquire()), ""
            finally:
                engine.dispose()

    async def once(self) -> bool:
        job = await asyncio.to_thread(
            self.repository.claim,
            self.clock(),
            self.backend.lease_seconds,
            self.backend.max_attempts,
        )
        if job is None:
            return False
        heartbeat = asyncio.create_task(self._heartbeat(job))
        try:
            if not self.repository.authorized(job):
                self.repository.finish(job, self.clock(), error="authorization_revoked")
                return True
            if job.kind == "recipe":
                request = RecipeRequest.model_validate(job.request)
                inputs = job.inputs
                if inputs is None:
                    inputs = await asyncio.to_thread(self.executor.prepare, request)
                    self.repository.pin(job, inputs, self.clock())
                result, html = await asyncio.to_thread(self.executor.execute, request, inputs)
            elif job.kind == "refresh":
                inputs = job.inputs or {"run_id": str(uuid4())}
                if job.inputs is None:
                    self.repository.pin(job, inputs, self.clock())
                result, html = await asyncio.to_thread(self._refresh, job, inputs["run_id"])
            else:
                raise ValueError("unsupported job kind")
            if not self.repository.authorized(job):
                self.repository.finish(job, self.clock(), error="authorization_revoked")
            else:
                self.repository.finish(job, self.clock(), result=result, html=html)
        except LeaseLost:
            logger.info("job_lease_lost", extra={"fields": {"job_id": job.job_id}})
        except Exception as exc:
            logger.exception("job_failed", extra={"fields": {"job_id": job.job_id}})
            try:
                self.repository.finish(
                    job,
                    self.clock(),
                    error=(
                        "invalid_or_unavailable_input"
                        if isinstance(exc, ValueError)
                        else "execution_failed"
                    ),
                )
            except LeaseLost:
                pass
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
        return True

    async def serve(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            if not await self.once():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=1)
                except TimeoutError:
                    pass
