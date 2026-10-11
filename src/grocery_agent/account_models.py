"""Account-state wire contracts shared by the backend and Python clients."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal, Self

from pydantic import Field, TypeAdapter, field_validator, model_validator

from grocery_agent.models.common import DomainModel, NonEmpty, PositiveAmount, StoreId

Language = Literal["cs", "en"]
Revision = Annotated[int, Field(strict=True, ge=0)]
PresetName = Annotated[str, Field(min_length=1, max_length=100)]
GRAMS_ADAPTER = TypeAdapter(PositiveAmount)
CondimentId = Literal[
    "salt", "black_pepper", "paprika", "dried_herbs", "cumin", "chilli", "garlic_powder"
]
Condiments = Annotated[tuple[CondimentId, ...], Field(max_length=7)]
RetailerIds = Annotated[tuple[StoreId, ...], Field(max_length=64)]
MaxStores = Annotated[int, Field(strict=True, ge=1, le=3)]


class ShoppingPreferences(DomainModel):
    retailer_ids: RetailerIds | None = None
    max_stores: MaxStores | None = None

    @field_validator("retailer_ids")
    @classmethod
    def unique_retailers(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is not None and len(set(value)) != len(value):
            raise ValueError("retailer IDs must be unique")
        return value


class RevisionInput(DomainModel):
    expected_revision: Revision


class SettingsView(ShoppingPreferences):
    revision: Revision = 0
    ui_language: Language | None = None
    recipe_language: Language | None = None


class SettingsPatch(RevisionInput, ShoppingPreferences):
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


class PantryCondiments(DomainModel):
    condiments: Condiments = ()

    @field_validator("condiments")
    @classmethod
    def unique_condiments(cls, value: tuple[CondimentId, ...]) -> tuple[CondimentId, ...]:
        if len(set(value)) != len(value):
            raise ValueError("condiments must be unique")
        return value


class PantryView(PantryCondiments):
    revision: Revision = 0
    items: PantryItems = Field(default_factory=dict)


class PantryPut(RevisionInput, PantryCondiments):
    items: PantryItems


class PresetCreate(DomainModel):
    name: PresetName
    parameters: dict[str, Any]

    @field_validator("parameters")
    @classmethod
    def reusable_parameters(cls, value: dict[str, Any]) -> dict[str, Any]:
        if {"pantry", "use_first", "request_id", "condiments"} & value.keys():
            raise ValueError(
                "preset parameters must exclude pantry, use_first, request_id and condiments"
            )
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


class SavedRecipeCreate(DomainModel):
    job_id: Annotated[str, Field(min_length=1, max_length=36)]
    # Semantic index validation follows ownership validation in the repository.
    meal_index: Annotated[int, Field(strict=True)]


class SavedRecipeView(DomainModel):
    id: str
    job_id: str
    meal_index: Annotated[int, Field(strict=True, ge=0)]
    created_at: datetime
    meal: dict[str, Any]


class SavedRecipeDetail(SavedRecipeView):
    request: dict[str, Any]
    result: dict[str, Any]


class SavedRecipePage(DomainModel):
    items: list[SavedRecipeView]
    total: Annotated[int, Field(ge=0)]
    offset: Annotated[int, Field(ge=0)]
    limit: Annotated[int, Field(ge=1, le=100)]
