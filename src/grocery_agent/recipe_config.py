"""Independent recipe options shared by the local CLI and recipe service."""

from pathlib import Path
from typing import Literal, Self

from pydantic import Field, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from grocery_agent.catalogue.profiles import AcquisitionProfile
from grocery_agent.configuration import MappingDefaultsSource, app_defaults, shared_defaults
from grocery_agent.models.common import StoreId


class RecipeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GROCERY_RECIPES_", extra="forbid", hide_input_in_errors=True
    )

    source_scopes: dict[StoreId, tuple[str, ...]] = Field(default_factory=dict)
    acquisition_profiles: tuple[AcquisitionProfile, ...] = ()
    context_ingredients: int = Field(default=60, ge=1, le=200)
    provider_timeout_seconds: int = Field(default=120, ge=1, le=600)
    default_provider: Literal["codex", "ollama", "template"] = "codex"
    default_cache_policy: Literal["local", "refresh", "no-cache", "cache-only"] = "local"
    model: str | None = None

    @field_validator("source_scopes")
    @classmethod
    def compatible_scopes(cls, value: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
        for scopes in value.values():
            if not scopes or len(set(scopes)) != len(scopes):
                raise ValueError("each source requires unique compatible scopes")
            if any(not scope.strip() or len(scope) > 500 for scope in scopes):
                raise ValueError("compatible scopes must be nonempty and bounded")
        return value

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
            MappingDefaultsSource(settings_cls, lambda: shared_defaults("recipes")),
        )

    @classmethod
    def load(cls, path: Path | None = None, *, shared_config: Path | None = None) -> Self:
        from grocery_agent.configuration import shared_config_context

        with shared_config_context(shared_config):
            return cls(**app_defaults("recipes", path, fields=cls.model_fields))
