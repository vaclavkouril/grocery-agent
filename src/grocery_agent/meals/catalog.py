from __future__ import annotations

import re
import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, HttpUrl, field_validator, model_validator

from grocery_agent.models.common import DomainModel, ExactDecimal, MoneyAmount, NonEmpty

Nonnegative = Annotated[ExactDecimal, Field(ge=0)]
Positive = Annotated[ExactDecimal, Field(gt=0)]
MealStyle = Literal["main", "breakfast", "snack"]


class Nutrients(DomainModel):
    kcal: Nonnegative
    protein_g: Nonnegative
    carbs_g: Nonnegative
    fat_g: Nonnegative

    def scaled(self, factor: Decimal) -> Nutrients:
        return Nutrients(
            kcal=self.kcal * factor,
            protein_g=self.protein_g * factor,
            carbs_g=self.carbs_g * factor,
            fat_g=self.fat_g * factor,
        )

    def plus(self, other: Nutrients) -> Nutrients:
        return Nutrients(
            kcal=self.kcal + other.kcal,
            protein_g=self.protein_g + other.protein_g,
            carbs_g=self.carbs_g + other.carbs_g,
            fat_g=self.fat_g + other.fat_g,
        )


class Ingredient(DomainModel):
    label: NonEmpty
    name_pattern: str | None = None
    exclude_pattern: str = (
        r"šunka|s kostí|obalovan|marinovan|ochucen|hotov|vařen|konzerv|mléčn|směs|omáčk|paštik"
    )
    lactose_free: bool
    edible_fraction: Annotated[ExactDecimal, Field(gt=0, le=1)] = Decimal(1)
    nutrients_per_100g_edible: Nutrients
    nutrition_source: HttpUrl
    pantry_price_per_kg_czk: MoneyAmount | None = None

    @field_validator("name_pattern", "exclude_pattern")
    @classmethod
    def valid_pattern(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError("invalid ingredient name pattern") from exc
        return value

    @model_validator(mode="after")
    def acquisition_or_pantry(self) -> Self:
        if (self.name_pattern is None) == (self.pantry_price_per_kg_czk is None):
            raise ValueError(
                "ingredient needs either an offer matcher or an explicit pantry estimate"
            )
        return self


class Recipe(DomainModel):
    title: NonEmpty
    meal_style: MealStyle = "main"
    minutes: int = Field(gt=0)
    # Raw purchased mass per serving; edible fractions account for vegetable trimming.
    grams: dict[str, Positive]
    steps: tuple[NonEmpty, ...] = Field(min_length=1)


class MealPolicy(DomainModel):
    source_id: NonEmpty
    scope: NonEmpty
    location_label: NonEmpty = "Praha"
    timezone: NonEmpty = "Europe/Prague"
    lactose_free: bool = True
    meal_style: MealStyle = "main"
    servings: int = Field(default=1, ge=1, le=20)
    min_protein_g: Nonnegative = Decimal(70)
    max_kcal: Positive = Decimal(850)
    max_minutes: int | None = Field(default=None, strict=True, ge=1, le=480)
    max_cost_per_serving_czk: Positive = Decimal(100)
    max_age_hours: int = Field(default=36, ge=1, le=168)
    allow_loyalty: bool = False
    # Empty means all retailers. A configured allowlist can limit shopping trips.
    retailers: tuple[str, ...] = ()
    max_stores: int = Field(default=2, ge=1, le=3)
    ranking: Literal["protein_per_czk", "protein"] = "protein_per_czk"
    seasoning_allowance_czk: MoneyAmount = Decimal(3)

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("unknown planning timezone") from exc
        return value


class PantryItem(DomainModel):
    # None explicitly means enough is already available for any suggested recipe.
    grams: Positive | None = None
    use_first: bool = False


class Pantry(DomainModel):
    items: dict[str, PantryItem] = Field(default_factory=dict)
    seasonings_available: bool = False

    def available_grams(self, ingredient_id: str, required: Decimal) -> Decimal:
        item = self.items.get(ingredient_id)
        if item is None:
            return Decimal(0)
        return required if item.grams is None else min(required, item.grams)


class MealStyleDefaults(DomainModel):
    min_protein_g: Nonnegative
    max_kcal: Positive
    max_cost_per_serving_czk: Positive


class MealCatalog(DomainModel):
    policy: MealPolicy
    pantry: Pantry = Field(default_factory=Pantry)
    style_defaults: dict[MealStyle, MealStyleDefaults] = Field(default_factory=dict)
    ingredients: dict[str, Ingredient]
    recipes: tuple[Recipe, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def known_ingredients(self) -> Self:
        if not self.pantry.items.keys() <= self.ingredients.keys():
            raise ValueError("pantry ingredients must exist in the catalog")
        for recipe in self.recipes:
            if not recipe.grams or not recipe.grams.keys() <= self.ingredients.keys():
                raise ValueError("recipe ingredients must exist in the catalog")
        return self

    @classmethod
    def load(cls, path: Path) -> MealCatalog:
        with path.open("rb") as file:
            return cls.model_validate(tomllib.load(file, parse_float=Decimal))
