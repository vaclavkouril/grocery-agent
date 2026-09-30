import argparse
import asyncio
import json
import logging
from collections.abc import Sequence

import httpx
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from grocery_agent.config import Settings
from grocery_agent.logging import configure_logging
from grocery_agent.matching.base import ExactGTINResolver
from grocery_agent.persistence.database import open_database
from grocery_agent.persistence.repository import SQLAlchemyOfferRepository
from grocery_agent.persistence.snapshots import FileSnapshotStore
from grocery_agent.pipeline.service import ScrapePipeline
from grocery_agent.stores.base import AdapterContext, StoreAdapter
from grocery_agent.stores.registry import default_registry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="grocery-agent")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("stores", help="list registered adapters")
    scrape = commands.add_parser("scrape", help="fetch, validate, and persist offers")
    scrape.add_argument("store", nargs="?")
    scrape.add_argument("--all", action="store_true", dest="all_stores")
    runs = commands.add_parser("runs", help="show recent scrape outcomes as JSON")
    runs.add_argument("--limit", type=int, default=10)
    return parser


async def scrape_stores(settings: Settings, adapters: Sequence[StoreAdapter]) -> int:
    engine = open_database(settings.database_url)
    try:
        pipeline = ScrapePipeline(
            SQLAlchemyOfferRepository(engine),
            FileSnapshotStore(settings.snapshot_dir),
            ExactGTINResolver(),
        )
        exit_code = 0
        async with httpx.AsyncClient(
            timeout=settings.http_timeout_seconds,
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
        ) as http:
            for adapter in adapters:
                result = await pipeline.run(adapter, AdapterContext(http=http))
                print(json.dumps(result.as_dict(), ensure_ascii=False))
                if result.status != "success":
                    exit_code = 1
        return exit_code
    finally:
        engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    registry = default_registry()
    if args.command == "stores":
        print("\n".join(registry.ids()))
        return 0
    if args.command == "scrape":
        if bool(args.store) == args.all_stores:
            parser.error("specify a store or --all")
        try:
            adapters = [
                registry.create(store)
                for store in (registry.ids() if args.all_stores else (args.store,))
            ]
        except ValueError as exc:
            parser.error(str(exc))
    elif args.limit <= 0:
        parser.error("--limit must be positive")
    try:
        settings = Settings()
        configure_logging(settings.log_level)
        if args.command == "scrape":
            return asyncio.run(scrape_stores(settings, adapters))
        engine = open_database(settings.database_url)
        try:
            print(
                json.dumps(
                    SQLAlchemyOfferRepository(engine).recent_runs(args.limit), ensure_ascii=False
                )
            )
            return 0
        finally:
            engine.dispose()
    except KeyboardInterrupt:
        return 130
    except (ValidationError, SQLAlchemyError, OSError) as exc:
        logging.getLogger(__name__).error(
            "command_failed",
            extra={
                "fields": {
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            },
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
