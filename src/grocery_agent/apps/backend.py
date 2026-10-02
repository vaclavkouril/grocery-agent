"""Independent backend, worker and local administrator startup."""

import argparse
import asyncio
import json
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

from grocery_agent.backend.config import BackendSettings
from grocery_agent.backend.recipes import RecipeExecutor
from grocery_agent.backend.repository import ControlRepository
from grocery_agent.backend.worker import Worker
from grocery_agent.config import Settings
from grocery_agent.configuration import shared_config_context
from grocery_agent.logging import configure_logging
from grocery_agent.models.common import utc_now
from grocery_agent.persistence.control.config import ControlSettings
from grocery_agent.persistence.database import create_database_engine
from grocery_agent.persistence.migrations import migration_config, schema_version


async def run_worker(worker: Worker, once: bool) -> None:
    if once:
        await worker.once()
        return
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    try:
        await worker.serve(stop)
    finally:
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(signum)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="grocery-backend")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--shared-config", type=Path)
    commands = parser.add_subparsers(dest="action", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    worker_parser = commands.add_parser("worker")
    worker_parser.add_argument("--once", action="store_true")
    bootstrap = commands.add_parser("bootstrap", help="locally provision an administrator session")
    bootstrap.add_argument("username")
    bootstrap.add_argument("--password-stdin", action="store_true")
    args = parser.parse_args(argv)
    with shared_config_context(args.shared_config):
        return _run(args, parser)


def _run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    settings = Settings()
    backend = BackendSettings.load(args.config)
    control = ControlSettings()
    if settings.database_url == control.database_url:
        parser.error("offer and control databases must be separate")
    configure_logging(settings.log_level)
    engine = create_database_engine(control.database_url)
    try:
        from alembic.script import ScriptDirectory

        head = ScriptDirectory.from_config(migration_config("control")).get_current_head()
        if schema_version(engine, "control") != head:
            parser.error("run grocery-agent db upgrade control before starting the backend")
        repository = ControlRepository(engine)
        if args.action == "bootstrap":
            from grocery_agent.backend.api import InvitationInput

            InvitationInput(username=args.username)
            password = None
            if args.password_stdin:
                from grocery_agent.backend.passwords import password_bytes

                password = sys.stdin.readline(4098).removesuffix("\n").removesuffix("\r")
                try:
                    password_bytes(password)
                except (ValueError, UnicodeError):
                    parser.error(
                        "password must contain 15..1024 characters and at most 4096 UTF-8 bytes"
                    )
            print(
                json.dumps(
                    {
                        "token": repository.bootstrap(
                            args.username, utc_now(), backend.session_hours, password=password
                        )
                    }
                )
            )
        elif args.action == "worker":
            executor = RecipeExecutor(settings, backend, utc_now)
            asyncio.run(
                run_worker(Worker(repository, executor, settings, backend, utc_now), args.once)
            )
        else:
            import uvicorn

            from grocery_agent.backend.api import create_app

            uvicorn.run(
                create_app(settings, backend, control, engine=engine),
                host=args.host or backend.host,
                port=args.port or backend.port,
                proxy_headers=False,
                log_config=None,
            )
    finally:
        engine.dispose()
    return 0


def worker_main() -> int:
    parser = argparse.ArgumentParser(prog="grocery-worker")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--shared-config", type=Path)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(sys.argv[1:])
    options = ["--config", str(args.config)] if args.config else []
    if args.shared_config is not None:
        options.extend(["--shared-config", str(args.shared_config)])
    return main([*options, "worker", *(["--once"] if args.once else [])])


if __name__ == "__main__":
    raise SystemExit(main())
