import asyncio
import logging
from collections.abc import Callable
from datetime import date, datetime
from zoneinfo import ZoneInfo

from grocery_agent.application.runtime import run_acquisition
from grocery_agent.collector.config import CollectorSettings
from grocery_agent.collector.schedule import next_collection
from grocery_agent.config import Settings
from grocery_agent.models.common import utc_now
from grocery_agent.pipeline.results import ScrapeResult
from grocery_agent.stores.registry import StoreRegistry
from grocery_agent.workflow import writer_lock

logger = logging.getLogger(__name__)


class CollectorService:
    def __init__(
        self,
        settings: Settings,
        schedule: CollectorSettings,
        registry: StoreRegistry,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.settings, self.schedule, self.registry, self.clock = (
            settings,
            schedule,
            registry,
            clock,
        )
        # Fail on unknown sources before starting a process that will otherwise wait for hours.
        for source in schedule.sources:
            registry.create(source)

    async def collect_once(self) -> tuple[ScrapeResult, ...]:
        results: list[ScrapeResult] = []
        failures = 0
        started = self.clock()
        with writer_lock(self.settings.lock_path):
            for source in self.schedule.sources:
                try:
                    (result,) = await run_acquisition(self.settings, [self.registry.create(source)])
                    results.append(result)
                    failures += int(result.status != "success")
                except Exception:
                    failures += 1
                    logger.exception(
                        "collector_source_failed", extra={"fields": {"source_id": source}}
                    )
        logger.info(
            "collector_round_finished",
            extra={
                "fields": {
                    "started_at": started.isoformat(),
                    "finished_at": self.clock().isoformat(),
                    "sources": self.schedule.sources,
                    "failed_sources": failures,
                    "accepted": sum(result.accepted for result in results),
                    "changed": sum(result.changed for result in results),
                }
            },
        )
        if failures and not results:
            raise ValueError("all collector sources failed; inspect structured logs")
        # A publication exception must not disappear behind successful results from other sources.
        if len(results) != len(self.schedule.sources):
            raise ValueError("collector round incomplete; inspect source publication errors")
        return tuple(results)

    async def serve(self) -> None:
        last_day: date | None = None
        if self.schedule.run_on_startup:
            await self._scheduled_round()
        while True:
            due = next_collection(
                self.clock(), self.schedule.local_time, self.schedule.timezone, last_day
            )
            logger.info(
                "collector_waiting",
                extra={
                    "fields": {
                        "next_run_at": due.isoformat(),
                        "timezone": self.schedule.timezone,
                        "sources": self.schedule.sources,
                    }
                },
            )
            # This waits for a wall-clock deadline, periodically accounting for clock changes.
            while (remaining := (due - self.clock()).total_seconds()) > 0:  # noqa: ASYNC110
                # Recheck the clock; cancellation remains immediate even during a long wait.
                await asyncio.sleep(min(remaining, 60))
            await self._scheduled_round()
            last_day = self.clock().astimezone(ZoneInfo(self.schedule.timezone)).date()

    async def _scheduled_round(self) -> None:
        try:
            await self.collect_once()
        except Exception:
            logger.exception("collector_round_failed")
        # A failed scheduled round leaves the previous catalogue intact and waits until
        # the next daily slot. Operators can explicitly request collector --once sooner.
