"""Command-line composition; domain services never print or open user storage."""

import argparse
import asyncio
import json
import logging
import signal
from collections.abc import Sequence
from contextlib import nullcontext
from uuid import UUID

from alembic.util.exc import CommandError
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from grocery_agent import workflow
from grocery_agent.application.commands import (
    MealCommand,
    ScrapeCommand,
    WorkflowCommand,
    parse_command,
)
from grocery_agent.application.parameters import (
    MealOverrides,
    PantryOverrides,
    PolicyOverrides,
    parse_owned_stock,
    resolve_parameters,
)
from grocery_agent.application.reports import FileMealReportStore
from grocery_agent.application.runtime import run_acquisition, run_meals, run_workflow
from grocery_agent.application.services import AcquisitionFailed, MealExecution
from grocery_agent.catalogue.models import CatalogueQuery
from grocery_agent.catalogue.repository import SQLAlchemyCatalogueRepository
from grocery_agent.collector.config import CollectorSettings
from grocery_agent.collector.service import CollectorService
from grocery_agent.config import Settings
from grocery_agent.logging import configure_logging
from grocery_agent.meals.catalog import MealCatalog
from grocery_agent.meals.report import save_failure
from grocery_agent.models.product import Unit
from grocery_agent.persistence.database import create_database_engine, open_database
from grocery_agent.persistence.migrations import (
    require_offer_schema,
    schema_version,
    upgrade_database,
)
from grocery_agent.persistence.reader import SQLAlchemyCurrentOfferReader
from grocery_agent.persistence.repository import SQLAlchemyOfferRepository
from grocery_agent.stores.base import AcquisitionAdapter
from grocery_agent.stores.registry import StoreRegistry, default_registry

logger = logging.getLogger(__name__)


def add_meal_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--meal-style", choices=("main", "breakfast", "snack"))
    parser.add_argument(
        "--have",
        action="append",
        default=[],
        metavar="INGREDIENT=QUANTITY",
        help="owned stock, e.g. rice=5kg, chicken=2000g, oil=available; repeatable",
    )
    parser.add_argument("--use-first", action="append", default=[], metavar="INGREDIENT")
    parser.add_argument("--have-seasonings", action="store_true", default=None)
    parser.add_argument("--servings", type=int)
    parser.add_argument("--min-protein", dest="min_protein_g")
    parser.add_argument("--max-kcal")
    parser.add_argument("--budget", dest="max_cost_per_serving_czk")
    parser.add_argument("--max-stores", type=int)
    parser.add_argument("--max-age-hours", type=int)
    parser.add_argument("--ranking", choices=("protein_per_czk", "protein"))
    parser.add_argument("--allow-loyalty", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--lactose-free", action=argparse.BooleanOptionalAction, default=None)
    retailers = parser.add_mutually_exclusive_group()
    retailers.add_argument("--retailer", action="append")
    retailers.add_argument("--all-retailers", action="store_true")
    parser.add_argument(
        "--no-latest",
        action="store_false",
        dest="publish_latest",
        default=True,
        help="write only isolated request artifacts; do not update the local latest shortcuts",
    )
    parser.add_argument("--strict-latest", action="store_true", help="refuse cached fallback")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="grocery-agent")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("stores", help="list registered acquisition sources")
    scrape = commands.add_parser("scrape", help="fetch, validate, persist and publish offers")
    scrape.add_argument("store", nargs="?")
    scrape.add_argument("--all", action="store_true", dest="all_stores")
    runs = commands.add_parser("runs", help="show recent scrape outcomes as JSON")
    runs.add_argument("--limit", type=int, default=10)
    for name, help_text in (
        ("meals", "plan from a fresh published catalogue without scraping"),
        ("workflow", "scrape the configured source, then plan from that exact batch"),
    ):
        meal = commands.add_parser(name, help=help_text)
        add_meal_options(meal)
        if name == "meals":
            meal.add_argument("--run-id", type=UUID, help="select a completed historical batch")
    command = commands.add_parser("command", help="run a structured text command")
    command.add_argument("text", help='e.g. "meal have=rice=5kg min_protein_g=70"')
    command.add_argument("--no-latest", action="store_false", dest="publish_latest", default=True)
    command.add_argument("--strict-latest", action="store_true")

    db = commands.add_parser("db", help="explicit independent database migrations")
    db.add_argument("operation", choices=("upgrade", "status"))
    db.add_argument("target", choices=("offers", "control"), nargs="?", default="offers")
    catalogue = commands.add_parser("catalogue", help="search the saved, expiry-aware catalogue")
    catalogue.add_argument("source")
    catalogue.add_argument("--scope")
    catalogue.add_argument("--category")
    catalogue.add_argument("--retailer", action="append", default=[])
    catalogue.add_argument("--search")
    catalogue.add_argument("--unit", choices=tuple(unit.value for unit in Unit))
    catalogue.add_argument("--max-unit-price")
    catalogue.add_argument("--sort", choices=("name", "unit_price"), default="name")
    catalogue.add_argument("--allow-loyalty", action="store_true")
    catalogue.add_argument("--limit", type=int, default=50)
    catalogue.add_argument("--offset", type=int, default=0)
    catalogue.add_argument("--rebuild", action="store_true", help="publish a saved complete run")
    catalogue.add_argument("--run-id", type=UUID, help="completed run used by --rebuild")
    collector = commands.add_parser("collector", help="independent scheduled catalogue collector")
    collector.add_argument("--once", action="store_true", help="collect once and exit")
    collector.add_argument("--config", help="collector TOML path")
    api = commands.add_parser("serve-catalogue", help="optional private read-only catalogue API")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8000)
    return parser


def meal_overrides(args: argparse.Namespace) -> MealOverrides:
    names = (
        "meal_style",
        "servings",
        "min_protein_g",
        "max_kcal",
        "max_cost_per_serving_czk",
        "max_stores",
        "max_age_hours",
        "ranking",
        "lactose_free",
        "allow_loyalty",
    )
    policy = {name: getattr(args, name) for name in names if getattr(args, name) is not None}
    if args.all_retailers:
        policy["retailers"] = ()
    elif args.retailer is not None:
        policy["retailers"] = tuple(args.retailer)
    return MealOverrides(
        policy=PolicyOverrides.model_validate(policy),
        pantry=PantryOverrides(
            items=parse_owned_stock(args.have),
            use_first=tuple(args.use_first),
            seasonings_available=args.have_seasonings,
        ),
    )


def pantry_overrides(catalog: MealCatalog, args: argparse.Namespace) -> MealCatalog:
    """Compatibility helper; parsing and resolution are shared across all channels."""
    return resolve_parameters(catalog, meal_overrides(args)).apply_to(catalog)


async def scrape_stores(settings: Settings, adapters: Sequence[AcquisitionAdapter]) -> int:
    results = await run_acquisition(settings, adapters)
    for result in results:
        print(json.dumps(result.as_dict(), ensure_ascii=False))
    return int(any(result.status != "success" for result in results))


def save_execution(settings: Settings, execution: MealExecution, publish_latest: bool) -> None:
    artifacts = FileMealReportStore(settings.report_dir).save(
        execution, publish_latest=publish_latest
    )
    logger.info(
        "meal_report_saved",
        extra={
            "fields": {
                "request_id": str(execution.request_id),
                "run_id": execution.report.run_id,
                "report": str(artifacts.html_path),
                "manifest": str(artifacts.request_path),
            }
        },
    )
    print(execution.report.model_dump_json(exclude_computed_fields=True))


def execute(
    settings: Settings,
    registry: StoreRegistry,
    command: MealCommand | WorkflowCommand | ScrapeCommand,
    *,
    publish_latest: bool = True,
    strict_latest: bool = False,
) -> int:
    if isinstance(command, ScrapeCommand):
        adapters = [registry.create(source) for source in command.source_ids]
        with workflow.writer_lock(settings.lock_path):
            return asyncio.run(scrape_stores(settings, adapters))
    catalog = MealCatalog.load(settings.meal_config)
    resolve_parameters(catalog, command.overrides)  # Before files, database writes or HTTP.
    lock = (
        workflow.writer_lock(settings.lock_path)
        if isinstance(command, WorkflowCommand) or publish_latest
        else nullcontext()
    )
    with lock:
        try:
            if isinstance(command, WorkflowCommand):
                adapter = registry.create(catalog.policy.source_id)
                if publish_latest:
                    save_failure(settings.report_dir, "Refreshing offers; new report pending")
                result = asyncio.run(
                    run_workflow(settings, catalog, command, adapter, workflow.utc_now)
                )
                print(json.dumps(result.acquisition.as_dict(), ensure_ascii=False))
                execution = result.meal
            else:
                execution = run_meals(
                    settings, catalog, command, workflow.utc_now, strict_latest=strict_latest
                )
            save_execution(settings, execution, publish_latest)
            return 0
        except (Exception, KeyboardInterrupt) as exc:
            if isinstance(exc, AcquisitionFailed):
                print(json.dumps(exc.result.as_dict(), ensure_ascii=False))
            if publish_latest:
                save_failure(settings.report_dir, "Meal workflow failed; inspect runs and logs")
            raise


def database_command(settings: Settings, args: argparse.Namespace) -> int:
    url = settings.database_url
    if args.target == "control":
        # Import and configure user storage only for an explicit control command.
        from grocery_agent.persistence.control.config import ControlSettings

        url = ControlSettings().database_url
    lock = (
        workflow.writer_lock(settings.lock_path) if args.operation == "upgrade" else nullcontext()
    )
    with lock:
        engine = create_database_engine(
            url, create_parent=args.operation == "upgrade", read_only=args.operation == "status"
        )
        try:
            revision = (
                upgrade_database(engine, args.target)
                if args.operation == "upgrade"
                else schema_version(engine, args.target)
            )
            print(json.dumps({"database": args.target, "revision": revision}))
        finally:
            engine.dispose()
    return 0


def catalogue_command(settings: Settings, args: argparse.Namespace) -> int:
    query = CatalogueQuery.model_validate(
        {
            "source_id": args.source,
            "scope": args.scope,
            "category": args.category,
            "retailers": tuple(args.retailer),
            "search": args.search,
            "unit": args.unit,
            "max_unit_price": args.max_unit_price,
            "sort": args.sort,
            "allow_loyalty": args.allow_loyalty,
            "limit": args.limit,
            "offset": args.offset,
        }
    )
    lock = workflow.writer_lock(settings.lock_path) if args.rebuild else nullcontext()
    with lock:
        engine = (
            open_database(settings.database_url)
            if args.rebuild
            else create_database_engine(settings.database_url, read_only=True)
        )
        try:
            require_offer_schema(engine)
            repository = SQLAlchemyCatalogueRepository(engine)
            if args.rebuild:
                reader = SQLAlchemyCurrentOfferReader(engine)
                batch = (
                    reader.get_batch(str(args.run_id), args.source)
                    if args.run_id is not None
                    else reader.latest_batch(args.source)
                )
                repository.publish(batch.run_id)
            print(repository.search(query, workflow.utc_now()).model_dump_json())
        finally:
            engine.dispose()
    return 0


async def run_collector(service: CollectorService, once: bool) -> int:
    if once:
        results = await service.collect_once()
        for result in results:
            print(json.dumps(result.as_dict(), ensure_ascii=False))
        return int(any(result.status != "success" for result in results))
    task = asyncio.current_task()
    assert task is not None
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        await service.serve()
    finally:
        loop.remove_signal_handler(signal.SIGTERM)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    registry = default_registry()
    if args.command == "stores":
        print("\n".join(registry.ids()))
        return 0
    if args.command == "scrape":
        if bool(args.store) == args.all_stores:
            parser.error("specify a source or --all")
        try:
            sources = registry.ids() if args.all_stores else (args.store,)
            for source in sources:
                registry.create(source)
        except ValueError as exc:
            parser.error(str(exc))
    elif args.command == "runs" and args.limit <= 0:
        parser.error("--limit must be positive")
    elif args.command == "catalogue" and args.run_id is not None and not args.rebuild:
        parser.error("--run-id requires --rebuild")
    elif args.command == "serve-catalogue" and not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    try:
        settings = Settings()
        configure_logging(settings.log_level)
        if args.command == "scrape":
            return execute(settings, registry, ScrapeCommand(source_ids=sources))
        if args.command in {"meals", "workflow", "command"}:
            if args.command == "command":
                command = parse_command(args.text)
            elif args.command == "workflow":
                command = WorkflowCommand(overrides=meal_overrides(args))
            else:
                command = MealCommand(overrides=meal_overrides(args), run_id=args.run_id)
            return execute(
                settings,
                registry,
                command,
                publish_latest=args.publish_latest,
                strict_latest=args.strict_latest,
            )
        if args.command == "db":
            return database_command(settings, args)
        if args.command == "catalogue":
            return catalogue_command(settings, args)
        if args.command == "collector":
            from pathlib import Path

            schedule = CollectorSettings.load(
                Path(args.config) if args.config else settings.collector_config
            )
            return asyncio.run(
                run_collector(CollectorService(settings, schedule, registry), args.once)
            )
        if args.command == "serve-catalogue":
            try:
                import uvicorn

                from grocery_agent.catalogue.api import create_app
            except ImportError as exc:
                raise ValueError("install the catalogue-api extra to serve HTTP") from exc
            uvicorn.run(
                create_app(settings),
                host=args.host,
                port=args.port,
                proxy_headers=False,
                log_config=None,
            )
            return 0
        engine = create_database_engine(settings.database_url, read_only=True)
        try:
            require_offer_schema(engine)
            print(
                json.dumps(
                    SQLAlchemyOfferRepository(engine).recent_runs(args.limit), ensure_ascii=False
                )
            )
        finally:
            engine.dispose()
        return 0
    except (KeyboardInterrupt, asyncio.CancelledError):
        return 130
    except (ValidationError, ValueError, CommandError, SQLAlchemyError, OSError) as exc:
        error = (
            str(exc.errors(include_input=False, include_url=False))
            if isinstance(exc, ValidationError)
            else str(exc)
        )
        logger.error(
            "command_failed", extra={"fields": {"error_type": type(exc).__name__, "error": error}}
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
