"""Independent email startup: grocery-email --config config/email.toml."""

import argparse
import sys
import time
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from pathlib import Path

from grocery_agent.channels.config import EmailConfig
from grocery_agent.channels.email import (
    ChannelAPI,
    EmailCredentials,
    IMAPTransport,
    Inbox,
    Mailer,
    SMTPTransport,
    delivery_once,
    intake_once,
)


def channel_client(api_url: str, token: str, channel: str) -> AbstractContextManager[ChannelAPI]:
    # Lazy import lets disabled deployments start before the shared client is installed.
    from grocery_agent.http_client import ChannelClient

    return ChannelClient(api_url, token, channel)


def main(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[[str, str, str], AbstractContextManager[ChannelAPI]] = channel_client,
    imap_factory: Callable[
        [EmailConfig, EmailCredentials], AbstractContextManager[Inbox]
    ] = IMAPTransport,
    smtp_factory: Callable[
        [EmailConfig, EmailCredentials], AbstractContextManager[Mailer]
    ] = SMTPTransport,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    parser = argparse.ArgumentParser(prog="grocery-email")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--shared-config", type=Path)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = EmailConfig.load(args.config, shared_config=args.shared_config)
        if not config.enabled or not (config.intake_enabled or config.delivery_enabled):
            return 0
        credentials = EmailCredentials.load(config)
        with client_factory(config.api_url, credentials.service_token, "email") as client:
            while True:
                failed = False
                if config.intake_enabled:
                    try:
                        with imap_factory(config, credentials) as inbox:
                            failed |= bool(intake_once(config, credentials, client, inbox))
                    except Exception:
                        failed = True
                if config.delivery_enabled:
                    try:
                        with smtp_factory(config, credentials) as mailer:
                            failed |= bool(delivery_once(config, client, mailer))
                    except Exception:
                        failed = True
                if failed:
                    print("Email transport round failed", file=sys.stderr)
                if args.once:
                    return int(failed)
                sleep(config.poll_seconds)
    except KeyboardInterrupt:
        return 0
    except Exception:
        print("Email startup failed; check configuration and service availability", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
