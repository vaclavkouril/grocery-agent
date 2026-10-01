"""Typed commands usable directly without accounts, a server or a queue."""

import shlex
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import Field, TypeAdapter, field_validator

from grocery_agent.application.parameters import (
    MealOverrides,
    PantryOverrides,
    PolicyOverrides,
    parse_owned_stock,
)
from grocery_agent.models.common import DomainModel, StoreId


class Request(DomainModel):
    version: Literal[1] = 1
    request_id: UUID = Field(default_factory=uuid4)


class MealCommand(Request):
    action: Literal["meals"] = "meals"
    overrides: MealOverrides = Field(default_factory=MealOverrides)
    run_id: UUID | None = None


class WorkflowCommand(Request):
    action: Literal["workflow"] = "workflow"
    overrides: MealOverrides = Field(default_factory=MealOverrides)


class ScrapeCommand(Request):
    action: Literal["scrape"] = "scrape"
    source_ids: Annotated[tuple[StoreId, ...], Field(min_length=1)]

    @field_validator("source_ids")
    @classmethod
    def unique_sources(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("source IDs must be unique")
        return value


Command = Annotated[MealCommand | WorkflowCommand | ScrapeCommand, Field(discriminator="action")]
COMMAND_SCHEMA: TypeAdapter[MealCommand | WorkflowCommand | ScrapeCommand] = TypeAdapter(Command)


def parse_command(text: str) -> MealCommand | WorkflowCommand | ScrapeCommand:
    """Parse a bounded allowlisted key=value grammar, never a shell command."""
    if len(text) > 4096:
        raise ValueError("command must be at most 4096 characters")
    tokens = shlex.split(text)
    if not tokens or len(tokens) > 64:
        raise ValueError("use meal, workflow or scrape followed by key=value parameters")
    action = {"meal": "meals", "meals": "meals", "workflow": "workflow", "scrape": "scrape"}.get(
        tokens[0]
    )
    if action is None:
        raise ValueError("unsupported command; use meal, workflow or scrape")
    values: dict[str, str] = {}
    for token in tokens[1:]:
        key, separator, value = token.partition("=")
        if not separator or not value or key in values:
            raise ValueError("parameters must be unique nonempty key=value pairs")
        values[key] = value
    if action == "scrape":
        if values.keys() != {"source"}:
            raise ValueError("scrape accepts source=ID or source=ID,ID")
        return ScrapeCommand(source_ids=tuple(values["source"].split(",")))
    pantry = PantryOverrides(
        items=parse_owned_stock(values.pop("have", "").split(",")) if "have" in values else {},
        use_first=tuple(values.pop("use_first", "").split(",")) if "use_first" in values else (),
        seasonings_available=(
            parse_bool(values.pop("have_seasonings")) if "have_seasonings" in values else None
        ),
    )
    run_id = values.pop("run_id", None)
    if action == "workflow" and run_id is not None:
        raise ValueError("workflow always acquires a new batch; run_id is only valid for meal")
    policy: dict[str, object] = {}
    integer_fields = {"servings", "max_stores", "max_age_hours"}
    boolean_fields = {"lactose_free", "allow_loyalty"}
    for key, value in values.items():
        if key in integer_fields:
            if not value.isascii() or not value.isdigit():
                raise ValueError(f"{key} must be an integer")
            policy[key] = int(value)
        elif key in boolean_fields:
            policy[key] = parse_bool(value)
        elif key == "retailers":
            policy[key] = tuple(value.split(","))
        else:
            policy[key] = value
    overrides = MealOverrides(policy=PolicyOverrides.model_validate(policy), pantry=pantry)
    if action == "workflow":
        return WorkflowCommand(overrides=overrides)
    return MealCommand(overrides=overrides, run_id=UUID(run_id) if run_id else None)


def parse_bool(value: str) -> bool:
    if value not in {"true", "false"}:
        raise ValueError("boolean parameters must be true or false")
    return value == "true"
