import re
from collections.abc import Sequence
from datetime import time
from pathlib import Path
from typing import Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from grocery_agent.catalogue.profiles import AcquisitionProfile
from grocery_agent.configuration import (
    MappingDefaultsSource,
    app_defaults,
    read_toml,
    shared_config_context,
    shared_defaults,
)
from grocery_agent.models.common import StoreId


class CollectorSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GROCERY_COLLECTOR_", env_file=".env", extra="ignore"
    )

    sources: tuple[StoreId, ...] = ("kupi",)
    daily_at: str = "02:00"
    timezone: str = "Europe/Prague"
    run_on_startup: bool = True
    profiles: tuple[AcquisitionProfile, ...] = ()
    # At most one scheduled round per local calendar day; no automatic retry storm.

    @field_validator("sources")
    @classmethod
    def valid_sources(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(set(value)) != len(value):
            raise ValueError("collector sources must be nonempty and unique")
        return value

    @field_validator("profiles")
    @classmethod
    def valid_profiles(
        cls, value: tuple[AcquisitionProfile, ...]
    ) -> tuple[AcquisitionProfile, ...]:
        if len({profile.fingerprint for profile in value}) != len(value):
            raise ValueError("collector profiles must be unique")
        if len({(p.source_id, p.name) for p in value}) != len(value):
            raise ValueError("collector profile source:name identities must be unique")
        return value

    @model_validator(mode="after")
    def profiles_match_sources(self) -> "CollectorSettings":
        if self.profiles and {p.source_id for p in self.profiles} - set(self.sources):
            raise ValueError("collector profiles must refer to configured sources")
        return self

    @field_validator("daily_at")
    @classmethod
    def valid_time(cls, value: str) -> str:
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("daily_at must be HH:MM in 24-hour format")
        return value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("unknown collector timezone") from exc
        return value

    @property
    def local_time(self) -> time:
        return time.fromisoformat(self.daily_at)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return env_settings, dotenv_settings, init_settings, file_secret_settings

    @classmethod
    def load(cls, path: Path) -> Self:
        payload = read_toml(path)
        unknown = payload.keys() - cls.model_fields.keys()
        if unknown:
            raise ValueError(f"unknown collector settings: {', '.join(sorted(unknown))}")
        return cls(**payload)


class ScrapeOptions(BaseModel):
    """Pure validation also used for CLI overrides, without re-reading the environment."""

    model_config = ConfigDict(extra="forbid")
    profiles: tuple[AcquisitionProfile, ...] = (AcquisitionProfile(source_id="kupi"),)

    @field_validator("profiles")
    @classmethod
    def valid_profiles(
        cls, value: tuple[AcquisitionProfile, ...]
    ) -> tuple[AcquisitionProfile, ...]:
        if not value:
            raise ValueError("at least one acquisition profile is required")
        CollectorSettings.valid_profiles(value)
        if any(not p.name or ":" in p.name or p.name != p.name.strip() for p in value):
            raise ValueError("profile names must be nonempty and contain no colon")
        return value


class CollectOptions(ScrapeOptions):
    daily_at: str = "02:00"
    timezone: str = "Europe/Prague"
    run_on_startup: bool = True

    @field_validator("daily_at")
    @classmethod
    def valid_time(cls, value: str) -> str:
        return CollectorSettings.valid_time(value)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        return CollectorSettings.valid_timezone(value)


class ScrapeSettings(ScrapeOptions, BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GROCERY_SCRAPE_", env_file=".env", extra="ignore")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        name = "collect" if cls.model_config["env_prefix"] == "GROCERY_COLLECT_" else "scrape"
        return (
            env_settings,
            dotenv_settings,
            init_settings,
            file_secret_settings,
            MappingDefaultsSource(settings_cls, lambda: shared_defaults(name)),
        )

    @classmethod
    def load(cls, path: Path | None = None, *, shared_config: Path | None = None) -> Self:
        name = "collect" if cls.model_config["env_prefix"] == "GROCERY_COLLECT_" else "scrape"
        with shared_config_context(shared_config):
            return cls(
                **app_defaults(
                    name,
                    path,
                    fields=cls.model_fields,
                    unknown_message="unknown acquisition app settings",
                )
            )


class CollectSettings(CollectOptions, ScrapeSettings):
    model_config = SettingsConfigDict(
        env_prefix="GROCERY_COLLECT_", env_file=".env", extra="ignore"
    )

    def schedule(self) -> CollectorSettings:
        # App settings have already been validated. Legacy collector environment settings
        # must not override the independent app's selection or explicit CLI overrides.
        return CollectorSettings.model_construct(
            sources=tuple(dict.fromkeys(p.source_id for p in self.profiles)),
            profiles=self.profiles,
            daily_at=self.daily_at,
            timezone=self.timezone,
            run_on_startup=self.run_on_startup,
        )


def select_profiles(
    configured: Sequence[AcquisitionProfile],
    names: Sequence[str] = (),
    sources: Sequence[str] = (),
) -> tuple[AcquisitionProfile, ...]:
    """Select named profiles or a default profile for each explicitly requested source."""
    if len(set(sources)) != len(sources):
        raise ValueError("source selections must be unique")
    selected: list[AcquisitionProfile] = []
    if names:
        for name in names:
            matches = [p for p in configured if name in (p.name, f"{p.source_id}:{p.name}")]
            if len(matches) != 1:
                raise ValueError("unknown or ambiguous profile; use source:name")
            selected.append(matches[0])
        if sources and set(sources) != {p.source_id for p in selected}:
            raise ValueError("source selection does not match selected profiles")
    elif sources:
        for source in sources:
            selected.append(
                next(
                    (p for p in configured if p.source_id == source and p.name == "default"),
                    AcquisitionProfile(source_id=source),
                )
            )
    else:
        selected.extend(configured)
    return ScrapeOptions(profiles=tuple(selected)).profiles
