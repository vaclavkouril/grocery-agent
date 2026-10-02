"""Independent opt-in local SimpleX application entrypoint."""

import argparse
from collections.abc import Sequence
from pathlib import Path

from grocery_agent.channels.config import SimplexSettings
from grocery_agent.channels.simplex import run


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Opt-in local SimpleX channel transport")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--shared-config", type=Path)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    try:
        return run(
            SimplexSettings.load(args.config, shared_config=args.shared_config), once=args.once
        )
    except (ValueError, ImportError, OSError):
        parser.exit(
            2,
            "SimpleX startup failed: check configuration, environment credentials "
            "and optional dependencies.\n",
        )
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
