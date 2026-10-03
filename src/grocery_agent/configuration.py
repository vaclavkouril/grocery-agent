"""Read-only TOML layering and portable defaults, independent of application imports."""

import os
import tomllib
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource


class MappingDefaultsSource(PydanticBaseSettingsSource):
    """Adapt a deferred defaults supplier to the typed Pydantic settings contract."""

    def __init__(
        self, settings_cls: type[BaseSettings], supplier: Callable[[], dict[str, Any]]
    ) -> None:
        super().__init__(settings_cls)
        self.supplier = supplier

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return self.supplier().get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return self.supplier()


_SHARED_PATH: ContextVar[Path | None] = ContextVar("grocery_shared_config", default=None)
PACKAGE_ROOT = Path(__file__).resolve().parent
SOURCE_CONFIG_ROOT = PACKAGE_ROOT.parent.parent / "config"
SHARED_FIELDS = frozenset(
    "database_url snapshot_dir meal_config report_dir lock_path collector_config "
    "http_timeout_seconds user_agent log_level".split()
)
_SCHEMAS = {
    "shared": SHARED_FIELDS,
    "recipes": frozenset(
        (
            "source_scopes acquisition_profiles context_ingredients provider_timeout_seconds "
            "default_provider default_cache_policy model"
        ).split()
    ),
    "backend": frozenset(
        (
            "providers codex_models ollama_models cache_policies source_scopes "
            "acquisition_profiles "
            "session_hours lease_seconds heartbeat_seconds max_attempts max_pending_per_user "
            "provider_timeout_seconds context_ingredients host port website_enabled frontend_dir "
            "email_enabled simplex_enabled delivery_lease_seconds delivery_max_attempts "
            "password_login_enabled registration_enabled cookie_sessions_enabled cookie_secure "
            "trusted_origin "
            "auth_rate_limit auth_rate_window_seconds refresh_cooldown_seconds"
        ).split()
    ),
    "scrape": frozenset({"profiles"}),
    "collect": frozenset("profiles daily_at timezone run_on_startup".split()),
    "email": frozenset(
        (
            "enabled intake_enabled delivery_enabled api_url imap_host imap_port imap_mailbox "
            "smtp_host smtp_port sender trusted_authserv_id trusted_ingress trusted_senders "
            "max_message_bytes max_command_chars batch_size poll_seconds timeout_seconds"
        ).split()
    ),
    "simplex": frozenset(
        (
            "enabled api_url ws_url user_id allowed_contact_ids history_count poll_seconds "
            "response_timeout reconnect_seconds reconnect_max_seconds"
        ).split()
    ),
}
_PATH_FIELDS = frozenset(
    "snapshot_dir meal_config report_dir lock_path collector_config frontend_dir".split()
)


def default_config(name: str) -> Path:
    """Prefer a local config, then wheel data, then an absolute checkout fallback."""
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ValueError("invalid configuration name")
    filename = name if name.endswith(".toml") else name + ".toml"
    local = Path.cwd() / "config" / filename
    if local.is_file():
        return local
    packaged = PACKAGE_ROOT / "defaults" / filename
    return packaged if packaged.is_file() else SOURCE_CONFIG_ROOT / filename


@contextmanager
def shared_config_context(path: Path | None) -> Iterator[None]:
    """Scope a CLI path without changing the process environment."""
    token = _SHARED_PATH.set(path if path is not None else _SHARED_PATH.get())
    try:
        yield
    finally:
        _SHARED_PATH.reset(token)


def shared_config_path(
    path: Path | None = None, *, environ: Mapping[str, str] | None = None
) -> Path | None:
    env = os.environ if environ is None else environ
    selected = path if path is not None else _SHARED_PATH.get()
    if selected is None:
        raw = env.get("GROCERY_SHARED_CONFIG")
        selected = Path(raw) if raw else None
    return selected.expanduser().resolve() if selected is not None else None


def _reject_credentials(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "password_login_enabled":
                if type(child) is not bool:
                    raise ValueError("password_login_enabled must be a boolean")
                continue
            if (
                any(
                    part in key.lower().split("_")
                    for part in (
                        "password",
                        "username",
                        "token",
                        "secret",
                        "credentials",
                        "credential",
                        "api-key",
                    )
                )
                or key.lower() == "api_key"
            ):
                raise ValueError("credentials belong in environment, never TOML")
            _reject_credentials(child)
    elif isinstance(value, list):
        for child in value:
            _reject_credentials(child)
    elif isinstance(value, str) and "://" in value:
        try:
            credential = urlsplit(value).username
        except ValueError:
            raise ValueError("invalid configuration URL") from None
        if credential is not None:
            raise ValueError("credentials belong in environment, never TOML")


def read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        payload = tomllib.load(stream)
    _reject_credentials(payload)
    return payload


def _relative_paths(payload: dict[str, Any], directory: Path) -> dict[str, Any]:
    result = dict(payload)
    for name in _PATH_FIELDS & result.keys():
        if result[name] is not None:
            runtime_path = Path(result[name]).expanduser()
            result[name] = (
                runtime_path if runtime_path.is_absolute() else (directory / runtime_path).resolve()
            )
    url = result.get("database_url")
    if isinstance(url, str) and url.startswith(("sqlite:///", "sqlite+pysqlite:///")):
        prefix, database_location = url.split(":///", 1)
        filename, separator, query = database_location.partition("?")
        if filename and filename != ":memory:" and not Path(filename).is_absolute():
            result["database_url"] = (
                prefix
                + ":///"
                + str((directory / filename).resolve())
                + (separator + query if separator else "")
            )
    return result


def shared_defaults(
    name: str, shared_config: Path | None = None, *, environ: Mapping[str, str] | None = None
) -> dict[str, Any]:
    path = shared_config_path(shared_config, environ=environ)
    if path is None:
        return {}
    payload = read_toml(path)
    if payload.keys() - _SCHEMAS.keys():
        raise ValueError("unknown shared configuration table")
    for table, values in payload.items():
        if not isinstance(values, dict) or values.keys() - _SCHEMAS[table]:
            raise ValueError("unknown shared configuration fields")
    return _relative_paths(payload.get(name, {}), path.parent)


def app_defaults(
    name: str,
    path: Path | None = None,
    *,
    shared_config: Path | None = None,
    fields: Mapping[str, Any] | set[str] | frozenset[str] | None = None,
    environ: Mapping[str, str] | None = None,
    unknown_message: str = "unknown application configuration fields",
) -> dict[str, Any]:
    """Layer TOML defaults; each file's paths resolve against its own directory.

    Environment values are applied afterward by the application's settings model.
    """
    values = shared_defaults(name, shared_config, environ=environ)
    selected = path if path is not None else default_config(name)
    if path is not None or selected.is_file():
        payload = read_toml(selected)
        allowed = fields if fields is not None else _SCHEMAS[name]
        unknown = payload.keys() - (allowed.keys() if isinstance(allowed, Mapping) else allowed)
        if unknown:
            raise ValueError(unknown_message)
        payload = _relative_paths(payload, selected.expanduser().resolve().parent)
        values.update(payload)
    return values
