"""Request-local preferences shared by the CLI and future command channels."""

import re
from collections.abc import Sequence
from decimal import Decimal
from typing import Annotated, Any, Literal, Self

from pydantic import (
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from grocery_agent.meals.catalog import MealCatalog, MealPolicy, MealStyle, Pantry, PantryItem
from grocery_agent.models.common import DomainModel, ExactDecimal, NonEmpty, PositiveAmount, StoreId


class PolicyOverrides(DomainModel):
    meal_style: MealStyle | None = None
    servings: Annotated[int | None, Field(strict=True, ge=1, le=20)] = None
    min_protein_g: Annotated[
        ExactDecimal | None, Field(ge=0, le=300, max_digits=16, decimal_places=6)
    ] = None
    max_kcal: Annotated[
        ExactDecimal | None, Field(gt=0, le=10000, max_digits=16, decimal_places=6)
    ] = None
    max_minutes: Annotated[int | None, Field(strict=True, ge=1, le=480)] = None
    max_cost_per_serving_czk: Annotated[
        ExactDecimal | None, Field(gt=0, max_digits=16, decimal_places=4)
    ] = None
    max_stores: Annotated[int | None, Field(strict=True, ge=1, le=3)] = None
    max_age_hours: Annotated[int | None, Field(strict=True, ge=1, le=168)] = None
    ranking: Literal["protein_per_czk", "protein"] | None = None
    lactose_free: Annotated[bool | None, Field(strict=True)] = None
    allow_loyalty: Annotated[bool | None, Field(strict=True)] = None
    retailers: tuple[StoreId, ...] | None = None

    @field_validator("retailers")
    @classmethod
    def unique_retailers(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is not None and len(set(value)) != len(value):
            raise ValueError("retailers must be unique")
        return value

    @model_validator(mode="after")
    def supplied_values(self) -> Self:
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("omit an override to inherit its value; explicit null is invalid")
        return self

    @model_serializer(mode="wrap")
    def serialize_overrides(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        # Omitted values inherit; keep serialized commands valid when parsed again.
        return {key: value for key, value in handler(self).items() if value is not None}


class StockOverride(DomainModel):
    # Explicit None means enough stock, corresponding to --have ingredient=available.
    grams: PositiveAmount | None
    use_first: bool | None = None


class PantryOverrides(DomainModel):
    items: dict[str, StockOverride] = Field(default_factory=dict)
    use_first: tuple[NonEmpty, ...] = ()
    seasonings_available: bool | None = None

    @field_validator("use_first")
    @classmethod
    def unique_priorities(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("use-first ingredients must be unique")
        return value


class MealOverrides(DomainModel):
    policy: PolicyOverrides = Field(default_factory=PolicyOverrides)
    pantry: PantryOverrides = Field(default_factory=PantryOverrides)


class MealParameters(DomainModel):
    """Fully resolved preferences; no user ID or account repository is required."""

    version: Literal[1] = 1
    policy: MealPolicy
    pantry: Pantry = Field(default_factory=Pantry)

    @classmethod
    def from_catalog(cls, catalog: MealCatalog) -> Self:
        return cls.model_validate(
            {"policy": catalog.policy.model_dump(), "pantry": catalog.pantry.model_dump()}
        )

    def apply_to(self, catalog: MealCatalog) -> MealCatalog:
        # Revalidation also checks catalog ingredient IDs; never mutate shared nested dicts.
        payload = catalog.model_dump(mode="json")
        payload.update(policy=self.policy.model_dump(mode="json"))
        payload.update(pantry=self.pantry.model_dump(mode="json"))
        return MealCatalog.model_validate(payload)


def parse_owned_stock(entries: Sequence[str]) -> dict[str, StockOverride]:
    items: dict[str, StockOverride] = {}
    for entry in entries:
        key, separator, quantity = entry.partition("=")
        key, quantity = key.strip(), quantity.strip().casefold()
        if not separator or not key or len(quantity) > 128:
            raise ValueError("--have needs INGREDIENT=QUANTITY, e.g. rice=5kg")
        if key in items:
            raise ValueError(f"duplicate --have ingredient: {key}")
        grams = None
        if quantity != "available":
            match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(kg|g)", quantity)
            if not match:
                raise ValueError("--have quantity must be positive grams/kg or 'available'")
            grams = Decimal(match[1]) * (1000 if match[2] == "kg" else 1)
        items[key] = StockOverride(grams=grams)
    return items


def resolve_parameters(
    catalog: MealCatalog,
    overrides: MealOverrides,
    profile: MealParameters | None = None,
) -> MealParameters:
    """Catalog defaults -> optional saved parameters -> per-request overrides."""
    base = profile or MealParameters.from_catalog(catalog)
    if (base.policy.source_id, base.policy.scope) != (
        catalog.policy.source_id,
        catalog.policy.scope,
    ):
        raise ValueError("profile source and scope must match the selected catalog")
    policy = base.policy.model_dump(mode="json")
    style = overrides.policy.meal_style
    if style is not None and style != base.policy.meal_style:
        limits = ("min_protein_g", "max_kcal", "max_cost_per_serving_czk")
        defaults = catalog.style_defaults.get(style)
        policy.update(
            defaults.model_dump(mode="json")
            if defaults is not None
            else {key: catalog.policy.model_dump(mode="json")[key] for key in limits}
        )
    policy.update(overrides.policy.model_dump(mode="json", exclude_none=True))
    items = {key: value.model_copy(deep=True) for key, value in base.pantry.items.items()}
    unknown = (items.keys() | overrides.pantry.items.keys() | set(overrides.pantry.use_first)) - (
        catalog.ingredients.keys()
    )
    if unknown:
        raise ValueError(
            f"unknown pantry ingredient(s): {', '.join(sorted(unknown))}; choose from: "
            f"{', '.join(catalog.ingredients)}"
        )
    for key, stock in overrides.pantry.items.items():
        previous = items.get(key, PantryItem())
        items[key] = PantryItem(
            grams=stock.grams,
            use_first=previous.use_first if stock.use_first is None else stock.use_first,
        )
    for key in overrides.pantry.use_first:
        if key not in items:
            raise ValueError("--use-first needs owned stock in --have or the pantry configuration")
        items[key] = PantryItem(grams=items[key].grams, use_first=True)
    seasonings = overrides.pantry.seasonings_available
    result = MealParameters(
        policy=MealPolicy.model_validate(policy),
        pantry=Pantry(
            items=items,
            seasonings_available=(
                base.pantry.seasonings_available if seasonings is None else seasonings
            ),
        ),
    )
    result.apply_to(catalog)
    return result
