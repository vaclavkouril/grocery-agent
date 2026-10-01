from pydantic_settings import BaseSettings, SettingsConfigDict


class ControlSettings(BaseSettings):
    # Only explicit control operations instantiate this settings object.
    model_config = SettingsConfigDict(
        env_prefix="GROCERY_CONTROL_", env_file=".env", extra="ignore"
    )

    database_url: str = "sqlite:///data/control.db"
