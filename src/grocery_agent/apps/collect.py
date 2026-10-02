"""Independent daily profile collector startup."""

import argparse
import asyncio
import json
from collections.abc import Sequence

from grocery_agent.apps.scrape import add_selection_options, run_once
from grocery_agent.collector.config import CollectOptions, CollectSettings, select_profiles
from grocery_agent.collector.service import CollectorService
from grocery_agent.config import Settings
from grocery_agent.logging import configure_logging
from grocery_agent.stores.registry import StoreRegistry, default_registry


def create_service(
    settings: Settings, options: CollectSettings, registry: StoreRegistry | None = None
) -> CollectorService:
    return CollectorService(
        settings, options.schedule(), registry if registry is not None else default_registry()
    )


async def run(service: CollectorService, once: bool) -> int:
    if once:
        return await run_once(service)
    await service.serve()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="grocery-collect")
    add_selection_options(parser)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--daily-at")
    parser.add_argument("--timezone")
    args = parser.parse_args(argv)
    try:
        options = CollectSettings.load(args.config, shared_config=args.shared_config)
        payload = options.model_dump()
        payload["profiles"] = select_profiles(options.profiles, args.profile, args.source)
        for name in ("daily_at", "timezone"):
            if (value := getattr(args, name)) is not None:
                payload[name] = value
        validated = CollectOptions.model_validate(payload)
        options = options.model_copy(
            update={name: getattr(validated, name) for name in CollectOptions.model_fields}
        )
        settings = Settings.load(shared_config=args.shared_config)
        configure_logging(settings.log_level)
        return asyncio.run(run(create_service(settings, options), args.once))
    except (Exception, KeyboardInterrupt):
        print(json.dumps({"status": "failed", "error": "collection app failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
