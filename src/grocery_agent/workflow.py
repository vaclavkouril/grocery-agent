import fcntl
import logging
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
def writer_lock(path: Path) -> Iterator[None]:
    """All CLI writers use one advisory process lock; the OS releases it on exit/crash."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as file:
        try:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("another acquisition/report workflow holds the writer lock") from exc
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
