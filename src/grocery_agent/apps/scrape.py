"""Independent profile-aware acquisition startup; no meal or account dependencies."""

import argparse
import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

from grocery_agent.collector.config import (
    CollectorSettings,
    ScrapeSettings,
    select_profiles,
)
from grocery_agent.collector.service import CollectorService, ProfileCollectionFailed
from grocery_agent.config import Settings
from grocery_agent.logging import configure_logging
from grocery_agent.stores.registry import StoreRegistry, default_registry


def create_service(
    settings: Settings, options: ScrapeSettings, registry: StoreRegistry | None = None
) -> CollectorService:
    schedule = CollectorSettings.model_construct(
        sources=tuple(dict.fromkeys(p.source_id for p in options.profiles)),
        profiles=options.profiles,
    )
    return CollectorService(
        settings, schedule, registry if registry is not None else default_registry()
    )


def add_selection_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path)
    parser.add_argument("--shared-config", type=Path)
    parser.add_argument("--profile", action="append", default=[], metavar="[SOURCE:]NAME")
    parser.add_argument("--source", action="append", default=[], metavar="SOURCE")


async def run_once(service: CollectorService) -> int:
    try:
        results = await service.collect_once()
    except ProfileCollectionFailed as exc:
        for result in exc.results:
            print(json.dumps(result.as_dict(), ensure_ascii=False))
        for profile in exc.failed_profiles:
            print(
                json.dumps(
                    {
                        "source_id": profile.source_id,
                        "profile_fingerprint": profile.fingerprint,
                        "acquisition_profile": profile.model_dump(mode="json"),
                        "status": "failed",
                        "error": "profile acquisition or publication failed",
                    },
                    ensure_ascii=False,
                )
            )
        return 1
    for result in results:
        print(json.dumps(result.as_dict(), ensure_ascii=False))
    return int(any(result.status != "success" for result in results))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="grocery-scrape")
    add_selection_options(parser)
    args = parser.parse_args(argv)
    try:
        options = ScrapeSettings.load(args.config, shared_config=args.shared_config)
        profiles = select_profiles(options.profiles, args.profile, args.source)
        options = options.model_copy(update={"profiles": profiles})
        settings = Settings.load(shared_config=args.shared_config)
        configure_logging(settings.log_level)
        return asyncio.run(run_once(create_service(settings, options)))
    except (Exception, KeyboardInterrupt):
        # Exception text and validation inputs may contain credentials or private URLs.
        print(json.dumps({"status": "failed", "error": "acquisition app failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
