import fcntl
import logging
import math
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from grocery_agent.application.commands import MealCommand
from grocery_agent.application.reports import FileMealReportStore
from grocery_agent.application.runtime import run_meals
from grocery_agent.config import Settings
from grocery_agent.meals.catalog import MealCatalog
from grocery_agent.meals.planner import MealReport
from grocery_agent.models.common import utc_now as utc_now


@contextmanager
def writer_lock(path: Path, *, timeout_seconds: float = 0) -> Iterator[None]:
    """All CLI writers use one advisory process lock; the OS releases it on exit/crash."""
    if not math.isfinite(timeout_seconds) or timeout_seconds < 0:
        raise ValueError("writer lock timeout must be finite and nonnegative")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as file:
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise ValueError(
                        "another acquisition/report workflow holds the writer lock"
                    ) from exc
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        try:
            yield
        finally:
            fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def generate_meals(settings: Settings, catalog: MealCatalog) -> MealReport:
    execution = run_meals(settings, catalog, MealCommand(), utc_now)
    artifacts = FileMealReportStore(settings.report_dir).save(execution, publish_latest=True)
    logging.getLogger(__name__).info(
        "meal_report_saved",
        extra={
            "fields": {
                "request_id": str(execution.request_id),
                "run_id": execution.report.run_id,
                "report": str(artifacts.html_path),
            }
        },
    )
    return execution.report
