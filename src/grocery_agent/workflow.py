import fcntl
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from grocery_agent.config import Settings
from grocery_agent.meals.catalog import MealCatalog
from grocery_agent.meals.planner import MealReport, plan_meals
from grocery_agent.meals.report import save_report
from grocery_agent.models.common import utc_now
from grocery_agent.persistence.database import open_database
from grocery_agent.persistence.reader import SQLAlchemyCurrentOfferReader


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
    engine = open_database(settings.database_url)
    try:
        report = plan_meals(SQLAlchemyCurrentOfferReader(engine), catalog, utc_now())
        save_report(settings.report_dir, report, catalog)
        logging.getLogger(__name__).info(
            "meal_report_finished",
            extra={
                "fields": {
                    "run_id": report.run_id,
                    "meals": len(report.meals),
                    "location": report.policy.location_label,
                    "report": str(settings.report_dir / "latest.html"),
                    "top_protein_g": str(report.meals[0].nutrients_per_serving.protein_g),
                    "top_cost_czk": str(report.meals[0].usage_cost_per_serving_czk),
                }
            },
        )
        return report
    finally:
        engine.dispose()
