"""Account-state wire contracts shared by the backend and Python clients."""

from decimal import Decimal
from typing import Annotated, Any, Literal, Self

from pydantic import Field, TypeAdapter, field_validator, model_validator

from grocery_agent.models.common import DomainModel, NonEmpty, PositiveAmount

Language = Literal["cs", "en"]
Revision = Annotated[int, Field(strict=True, ge=0)]
PresetName = Annotated[str, Field(min_length=1, max_length=100)]
GRAMS_ADAPTER = TypeAdapter(PositiveAmount)


class RevisionInput(DomainModel):
    expected_revision: Revision


class SettingsView(DomainModel):
    revision: Revision = 0
    ui_language: Language | None = None
    recipe_language: Language | None = None


class SettingsPatch(RevisionInput):
    ui_language: Language | None = None
    recipe_language: Language | None = None


class PantryItem(DomainModel):
    grams: Annotated[str | None, Field(max_length=100)]
    use_first: Annotated[bool, Field(strict=True)] = False

    @field_validator("grams")
    @classmethod
    def canonical_grams(cls, value: str | None) -> str | None:
        if value is None:
            return None
        import re

        if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value) is None:
            raise ValueError("grams must be a positive decimal string or null for enough")
        amount = Decimal(value)
        if amount <= 0:
            raise ValueError("grams must be positive")
        # Work directly on decimal text; Decimal.normalize() can round large amounts.
        integer, _, fraction = format(amount, "f").partition(".")
        fraction = fraction.rstrip("0")
        canonical = integer + ("." + fraction if fraction else "")
        # Match recipe StockOverride precision after removing insignificant zeros.
        GRAMS_ADAPTER.validate_python(canonical)
        return canonical


PantryItems = Annotated[dict[NonEmpty, PantryItem], Field(max_length=200)]


class PantryView(DomainModel):
    revision: Revision = 0
    items: PantryItems = Field(default_factory=dict)


class PantryPut(RevisionInput):
    items: PantryItems


class PresetCreate(DomainModel):
    name: PresetName
    parameters: dict[str, Any]

    @field_validator("parameters")
    @classmethod
    def reusable_parameters(cls, value: dict[str, Any]) -> dict[str, Any]:
        if {"pantry", "use_first", "request_id"} & value.keys():
            raise ValueError("preset parameters must exclude pantry, use_first and request_id")
        return value


class PresetUpdate(RevisionInput):
    name: PresetName | None = None
    parameters: dict[str, Any] | None = None

    @model_validator(mode="after")
    def valid_patch(self) -> Self:
        if not self.model_fields_set & {"name", "parameters"}:
            raise ValueError("provide name or parameters")
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("name cannot be null")
        if "parameters" in self.model_fields_set:
            if self.parameters is None:
                raise ValueError("parameters cannot be null")
            PresetCreate.reusable_parameters(self.parameters)
        return self


class PresetView(DomainModel):
    id: str
    name: PresetName
    revision: Revision
    parameters: dict[str, Any]
    stale: bool = False


class PresetPage(DomainModel):
    items: list[PresetView]
    total: Annotated[int, Field(ge=0)]
