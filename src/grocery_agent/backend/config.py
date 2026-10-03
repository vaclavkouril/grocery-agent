from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from grocery_agent.catalogue.profiles import AcquisitionProfile
from grocery_agent.configuration import MappingDefaultsSource, app_defaults, shared_defaults
from grocery_agent.models.common import StoreId


class BackendSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GROCERY_BACKEND_", extra="forbid", hide_input_in_errors=True
    )

    providers: tuple[Literal["codex", "ollama", "template"], ...] = ("codex", "template")
    codex_models: tuple[str, ...] = ()
    ollama_models: tuple[str, ...] = ()
    cache_policies: tuple[Literal["local", "refresh", "no-cache", "cache-only"], ...] = Field(
        default=("cache-only",), min_length=1
    )
    source_scopes: dict[StoreId, tuple[str, ...]] = Field(default_factory=dict)
    acquisition_profiles: tuple[AcquisitionProfile, ...] = ()
    session_hours: int = Field(default=24, ge=1, le=720)
    password_login_enabled: bool = False
    registration_enabled: bool = False
    cookie_sessions_enabled: bool = False
    cookie_secure: bool = True
    trusted_origin: str | None = None
    auth_rate_limit: int = Field(default=10, ge=1, le=100)
    auth_rate_window_seconds: int = Field(default=300, ge=10, le=3600)
    refresh_cooldown_seconds: int = Field(default=300, ge=0, le=86400)
    lease_seconds: int = Field(default=180, ge=10, le=3600)
    heartbeat_seconds: int = Field(default=15, ge=1, le=60)
    max_attempts: int = Field(default=3, ge=1, le=10)
    max_pending_per_user: int = Field(default=10, ge=1, le=100)
    provider_timeout_seconds: int = Field(default=120, ge=1, le=600)
    context_ingredients: int = Field(default=60, ge=1, le=200)
    host: str = "127.0.0.1"
    port: int = Field(default=8001, ge=1, le=65535)
    website_enabled: bool = True
    frontend_dir: Path | None = None
    email_enabled: bool = False
    simplex_enabled: bool = False
    email_transport_token: SecretStr | None = Field(default=None, exclude=True)
    simplex_transport_token: SecretStr | None = Field(default=None, exclude=True)
    delivery_lease_seconds: int = Field(default=120, ge=10, le=3600)
    delivery_max_attempts: int = Field(default=5, ge=1, le=20)

    @property
    def default_cache_policy(self) -> str:
        return "cache-only" if "cache-only" in self.cache_policies else self.cache_policies[0]

    def allowed_source_scopes(self, source_id: str, scope: str) -> dict[str, tuple[str, ...]]:
        return dict(self.source_scopes) if self.source_scopes else {source_id: (scope,)}

    @field_validator("source_scopes")
    @classmethod
    def explicit_scopes(cls, value: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
        for scopes in value.values():
            if not scopes or len(set(scopes)) != len(scopes):
                raise ValueError("each source needs unique explicit compatible scopes")
            if any(not scope.strip() or len(scope) > 500 for scope in scopes):
                raise ValueError("compatible scopes must be nonempty and bounded")
        return value

    @model_validator(mode="after")
    def unique_permissions(self) -> Self:
        if self.registration_enabled and not self.password_login_enabled:
            raise ValueError("self-registration requires password_login_enabled")
        if self.trusted_origin is not None:
            parsed = urlsplit(self.trusted_origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("trusted_origin must be an HTTP(S) origin without a path")
            if not self.cookie_secure and (
                parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            ):
                raise ValueError("insecure cookies require an explicit loopback HTTP origin")
        if self.cookie_sessions_enabled and self.trusted_origin is None:
            raise ValueError("cookie sessions require trusted_origin")
        if not self.cookie_secure and self.trusted_origin is None:
            raise ValueError("insecure cookies require an explicit loopback HTTP origin")
        if len(set(self.cache_policies)) != len(self.cache_policies):
            raise ValueError("cache policies must be unique")
        fingerprints = [profile.fingerprint for profile in self.acquisition_profiles]
        if len(set(fingerprints)) != len(fingerprints):
            raise ValueError("acquisition profiles must be unique")
        if self.source_scopes:
            for profile in self.acquisition_profiles:
                scopes = self.source_scopes.get(profile.source_id, ())
                if not scopes or (profile.scope is not None and profile.scope not in scopes):
                    raise ValueError("acquisition profile must use a configured source and scope")
        return self

    @field_validator("email_transport_token", "simplex_transport_token", mode="before")
    @classmethod
    def private_transport_token(cls, value: str | SecretStr | None) -> SecretStr | None:
        token = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not token:
            return None
        if not 20 <= len(token) <= 200 or not token.isascii() or any(c.isspace() for c in token):
            raise ValueError(
                "transport credential must contain 20..200 non-whitespace ASCII characters"
            )
        return SecretStr(token)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            env_settings,
            init_settings,
            file_secret_settings,
            MappingDefaultsSource(settings_cls, lambda: shared_defaults("backend")),
        )

    @classmethod
    def load(cls, path: Path | None = None) -> Self:
        values = app_defaults("backend", path, fields=cls.model_fields)
        if {"email_transport_token", "simplex_transport_token"} & values.keys():
            raise ValueError("transport credentials must be supplied through environment variables")
        return cls(**values)
