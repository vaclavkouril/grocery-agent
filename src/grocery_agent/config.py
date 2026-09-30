from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GROCERY_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///data/grocery.db"
    snapshot_dir: Path = Path("data/snapshots")
    meal_config: Path = Path("config/meals.toml")
    report_dir: Path = Path("data/reports")
    lock_path: Path = Path("data/workflow.lock")
    http_timeout_seconds: float = Field(default=30, gt=0, le=300)
    user_agent: str = Field(default="grocery-agent/0.1", min_length=1)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
