from pathlib import Path
from typing import Any, Literal, Self

from pydantic import Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from grocery_agent.configuration import (
    MappingDefaultsSource,
    _relative_paths,
    default_config,
    shared_config_context,
    shared_config_path,
    shared_defaults,
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GROCERY_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///data/grocery.db"
    snapshot_dir: Path = Path("data/snapshots")
    meal_config: Path = Field(default_factory=lambda: default_config("meals"))
    report_dir: Path = Path("data/reports")
    lock_path: Path = Path("data/workflow.lock")
    collector_config: Path = Field(default_factory=lambda: default_config("collector"))
    http_timeout_seconds: float = Field(default=30, gt=0, le=300)
    user_agent: str = Field(default="grocery-agent/0.1", min_length=1)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        def shared() -> dict[str, Any]:
            values = shared_defaults("shared")
            path = shared_config_path()
            if path is not None:
                defaults = {
                    name: cls.model_fields[name].default
                    for name in ("database_url", "snapshot_dir", "report_dir", "lock_path")
                }
                values = {**_relative_paths(defaults, path.parent), **values}
            return values

        return (
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
            MappingDefaultsSource(settings_cls, shared),
        )

    @classmethod
    def load(cls, *, shared_config: Path | None = None, **overrides: Any) -> Self:
        with shared_config_context(shared_config):
            return cls(**overrides)
