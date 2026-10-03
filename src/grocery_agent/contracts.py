"""Transport contracts shared by HTTP clients and backend OpenAPI responses."""

from typing import Any

from pydantic import AwareDatetime, Field

from grocery_agent.models.common import DomainModel


class Principal(DomainModel):
    user_id: str
    username: str
    role: str


class JobView(DomainModel):
    job_id: str
    kind: str
    status: str
    phase: str
    attempts: int
    created_at: AwareDatetime
    updated_at: AwareDatetime
    error_code: str | None


class JobPage(DomainModel):
    items: tuple[JobView, ...]
    total: int
    offset: int
    limit: int


class Capabilities(DomainModel):
    providers: tuple[str, ...]
    default_provider: str | None = None
    models: dict[str, tuple[str, ...]]
    cache_policies: tuple[str, ...]
    default_cache_policy: str
    sources: tuple[str, ...]
    source_scopes: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    combined_sources: bool = False
    scope: str
    location_label: str | None = None
    ingredients: tuple[str, ...]
    ingredient_labels: dict[str, str] = Field(default_factory=dict)
    meal_styles: tuple[str, ...]
    refresh_allowed: bool
    refresh_profiles: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    password_login: bool = False
    registration: bool = False
    cookie_sessions: bool = False
    session_modes: tuple[str, ...] = ("bearer",)
    email_enabled: bool
    simplex_enabled: bool
    recipe_request_schema: dict[str, Any] = Field(default_factory=dict)
    offer_query_schema: dict[str, Any] = Field(default_factory=dict)
