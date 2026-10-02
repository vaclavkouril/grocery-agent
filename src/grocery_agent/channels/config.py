"""Shared configuration adapters for the independent channel transports."""

import json
import os
from collections.abc import Mapping
from dataclasses import fields
from pathlib import Path
from typing import Self

from grocery_agent.channels.email import EmailConfig as TransportEmailConfig
from grocery_agent.channels.simplex import SimplexSettings as TransportSimplexSettings
from grocery_agent.configuration import app_defaults


class EmailConfig(TransportEmailConfig):
    @classmethod
    def load(
        cls,
        path: Path | None = None,
        *,
        environ: Mapping[str, str] | None = None,
        shared_config: Path | None = None,
    ) -> Self:
        names = {field.name for field in fields(cls)}
        values = app_defaults(
            "email",
            path,
            shared_config=shared_config,
            fields=names,
            environ=environ,
            unknown_message=(
                "unknown email configuration fields (credentials belong in environment)"
            ),
        )
        env = os.environ if environ is None else environ
        defaults = cls()
        for name in names:
            raw = env.get("GROCERY_EMAIL_" + name.upper())
            if raw is None:
                continue
            default = getattr(defaults, name)
            if isinstance(default, bool):
                if raw.lower() not in {"true", "false", "1", "0"}:
                    raise ValueError("invalid email boolean environment value")
                values[name] = raw.lower() in {"true", "1"}
            elif isinstance(default, int):
                values[name] = int(raw)
            elif isinstance(default, float):
                values[name] = float(raw)
            elif isinstance(default, tuple):
                values[name] = json.loads(raw)
            else:
                values[name] = raw
        return cls(**values)


class SimplexSettings(TransportSimplexSettings):
    @classmethod
    def load(
        cls,
        path: Path | None = None,
        *,
        environ: Mapping[str, str] | None = None,
        shared_config: Path | None = None,
    ) -> Self:
        values = app_defaults(
            "simplex",
            path,
            shared_config=shared_config,
            fields=cls.model_fields,
            environ=environ,
        )
        env = os.environ if environ is None else environ
        for name in cls.model_fields:
            raw = env.get("GROCERY_SIMPLEX_" + name.upper())
            if raw is not None:
                values[name] = raw if name in {"api_url", "ws_url"} else json.loads(raw)
        return cls.model_validate(values)
