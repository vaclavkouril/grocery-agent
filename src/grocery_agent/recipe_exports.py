"""Export one saved meal for food logging, using only its immutable recipe data."""

import json
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Any, Literal

from pydantic import ValidationError

from grocery_agent.meals.planner import MealLine, PlannedMeal
from grocery_agent.models.offer import Offer
from grocery_agent.models.product import StoreProduct

ExportFormat = Literal["text", "ingredients", "schema-org"]
ExportLanguage = Literal["cs", "en"]


@dataclass(frozen=True)
class RecipeExport:
    body: str
    media_type: str
    extension: str


def _decimal(value: Decimal) -> str:
    """Plain decimal notation, without context-sensitive normalization or rounding."""
    if value == 0:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _whole(value: Decimal, servings: int) -> Decimal:
    with localcontext() as context:
        context.prec = max(28, len(value.as_tuple().digits) + len(str(servings)))
        return value * servings


def _reject_floats(value: Any) -> None:
    if isinstance(value, float):
        raise ValueError("saved recipe numbers must be exact decimals, not floats")
    if isinstance(value, dict):
        for item in value.values():
            _reject_floats(item)
    elif isinstance(value, (tuple, list)):
        for item in value:
            _reject_floats(item)


def _meal(result: dict[str, Any], index: int) -> PlannedMeal:
    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
        raise ValueError("meal_index must be a nonnegative integer")
    if not isinstance(result, dict) or not isinstance(result.get("report"), dict):
        raise ValueError("saved recipe must contain report.meals")
    meals = result["report"].get("meals")
    if not isinstance(meals, (list, tuple)) or index >= len(meals):
        raise ValueError("meal_index must select an existing saved recipe")
    selected = meals[index]
    _reject_floats(selected)
    if isinstance(selected, dict) and any(
        isinstance(selected.get(field), bool) for field in ("servings", "minutes")
    ):
        raise ValueError("saved recipe servings and minutes must be integers, not booleans")
    # Saved report JSON includes computed offer/product display properties that
    # their strict input models do not accept. Drop only those known properties,
    # on copies; preserve all recipe data and validate unknown extras normally.
    if isinstance(selected, dict) and isinstance(selected.get("lines"), (list, tuple)):
        selected = dict(selected)
        selected["lines"] = [_input_line(line) for line in selected["lines"]]
    try:
        meal = PlannedMeal.model_validate(selected)
    except ValidationError as exc:
        raise ValueError("selected saved recipe is malformed") from exc
    if meal.servings <= 0 or meal.minutes < 0 or not meal.title or not meal.lines:
        raise ValueError(
            "selected saved recipe needs a title, positive servings, nonnegative minutes and lines"
        )
    for line in meal.lines:
        amounts = (line.required_grams, line.edible_grams, line.owned_grams, line.purchased_grams)
        if any(not value.is_finite() or value < 0 for value in amounts):
            raise ValueError("saved recipe weights must be finite nonnegative decimals")
        if line.edible_grams > line.required_grams:
            raise ValueError("saved recipe edible weight must not exceed raw required weight")
    nutrients = meal.nutrients_per_serving
    if any(
        not value.is_finite()
        for value in (nutrients.kcal, nutrients.protein_g, nutrients.carbs_g, nutrients.fat_g)
    ):
        raise ValueError("saved recipe nutrition must be finite")
    return meal


def _input_line(line: Any) -> Any:
    if isinstance(line, dict) and any(
        isinstance(line.get(field), bool)
        for field in ("required_grams", "edible_grams", "owned_grams", "purchased_grams")
    ):
        raise ValueError("saved recipe weights must be decimals, not booleans")
    if not isinstance(line, dict) or not isinstance(line.get("price"), dict):
        return line
    line = dict(line)
    price = dict(line["price"])
    line["price"] = price
    if isinstance(price.get("offer"), dict):
        offer = {
            key: value
            for key, value in price["offer"].items()
            if key not in Offer.model_computed_fields
        }
        price["offer"] = offer
        if isinstance(offer.get("product"), dict):
            offer["product"] = {
                key: value
                for key, value in offer["product"].items()
                if key not in StoreProduct.model_computed_fields
            }
    return line


def _label(result: dict[str, Any], line: MealLine, language: ExportLanguage) -> str:
    label = line.price.label
    snapshot = result.get("ingredient_snapshot")
    if isinstance(snapshot, dict):
        ingredient = snapshot.get(line.price.ingredient_id)
        if isinstance(ingredient, dict) and isinstance(ingredient.get("labels"), dict):
            localized = ingredient["labels"].get(language)
            if isinstance(localized, str) and localized.strip():
                label = localized
    # Food-logging lists require exactly one line per ingredient, including for
    # saved labels containing newlines. No HTML or Markdown is interpreted.
    label = " ".join(label.split())
    if not label:
        raise ValueError("selected ingredient needs a saved label")
    # Exported grams are edible, so these known raw-label qualifiers would
    # misleadingly describe pre-trimming weights. Keep the raw state intact.
    for original, edible in (
        ("(raw, before trimming)", "(raw)"),
        ("(raw, before peeling)", "(raw)"),
        ("(syrová, před očištěním)", "(syrová)"),
        ("(syrová, před oloupáním)", "(syrová)"),
    ):
        label = label.replace(original, edible)
    return label


def export_recipe(
    result: dict[str, Any],
    meal_index: int,
    *,
    format: ExportFormat = "text",
    language: ExportLanguage = "cs",
) -> RecipeExport:
    """Export all servings of one meal; schema nutrition remains per serving.

    Frozen ``ingredient_snapshot[id].labels[language]`` overrides the saved price
    label when available. Current catalogues, offers, and account data are never
    consulted. Unknown condiment amounts and cooked yield are never inferred.
    """
    if format not in {"text", "ingredients", "schema-org"}:
        raise ValueError("format must be text, ingredients, or schema-org")
    if language not in {"cs", "en"}:
        raise ValueError("language must be cs or en")
    meal = _meal(result, meal_index)
    lines = tuple(line for line in meal.lines if line.edible_grams > 0)
    ingredients = [
        f"{_decimal(line.edible_grams)} g {_label(result, line, language)}" for line in lines
    ]
    if format == "ingredients":
        return RecipeExport("\n".join(ingredients), "text/plain; charset=utf-8", "txt")
    warning = (
        "Hmotnosti představují syrovou jedlou část, nikoli hmotnost po uvaření. "
        "Hmotnost hotového jídla není známa; výživové hodnoty na 100 g nelze určit. "
        "Výživové hodnoty koření bez uvedeného množství nejsou zahrnuty."
        if language == "cs"
        else "Weights are raw edible amounts, not cooked weights. "
        "Cooked yield is unknown; nutrition per 100 g cannot be determined. "
        "Nutrition from condiments without recorded amounts is omitted."
    )
    nutrients = meal.nutrients_per_serving
    if format == "schema-org":
        document = {
            "@context": "https://schema.org",
            "@type": "Recipe",
            "name": meal.title,
            "recipeYield": str(meal.servings),
            "totalTime": f"PT{meal.minutes}M",
            "description": warning,
            "recipeIngredient": ingredients,
            "recipeInstructions": [{"@type": "HowToStep", "text": step} for step in meal.steps],
            "nutrition": {
                "@type": "NutritionInformation",
                "servingSize": "1 porce" if language == "cs" else "1 serving",
                "calories": f"{_decimal(nutrients.kcal)} kcal",
                "proteinContent": f"{_decimal(nutrients.protein_g)} g",
                "carbohydrateContent": f"{_decimal(nutrients.carbs_g)} g",
                "fatContent": f"{_decimal(nutrients.fat_g)} g",
            },
        }
        return RecipeExport(
            json.dumps(document, ensure_ascii=False, indent=2), "application/ld+json", "json"
        )
    czech = language == "cs"
    rows = [
        ("Recept: " if czech else "Recipe: ") + meal.title,
        ("Počet porcí: " if czech else "Servings: ") + str(meal.servings),
        ("Doba přípravy: " if czech else "Preparation time: ") + f"{meal.minutes} min",
        "",
        "Suroviny pro celý recept (syrová jedlá část)"
        if czech
        else "Ingredients for the whole recipe (raw edible weights)",
    ]
    raw_label = "syrová hmotnost před očištěním" if czech else "raw weight before trimming"
    rows.extend(
        f"{ingredient} ({raw_label}: {_decimal(line.required_grams)} g)"
        for ingredient, line in zip(ingredients, lines, strict=True)
    )
    nutrient_rows = (
        ("Energie" if czech else "Energy", nutrients.kcal, "kcal"),
        ("Bílkoviny" if czech else "Protein", nutrients.protein_g, "g"),
        ("Sacharidy" if czech else "Carbohydrates", nutrients.carbs_g, "g"),
        ("Tuky" if czech else "Fat", nutrients.fat_g, "g"),
    )
    rows.extend(["", "Výživové hodnoty na porci" if czech else "Nutrition per serving"])
    rows.extend(f"{label}: {_decimal(value)} {unit}" for label, value, unit in nutrient_rows)
    rows.extend(
        ["", "Výživové hodnoty celého receptu" if czech else "Nutrition for the whole recipe"]
    )
    rows.extend(
        f"{label}: {_decimal(_whole(value, meal.servings))} {unit}"
        for label, value, unit in nutrient_rows
    )
    rows.extend(["", "Postup" if czech else "Steps"])
    rows.extend(f"{index}. {step}" for index, step in enumerate(meal.steps, 1))
    rows.extend(["", "Upozornění" if czech else "Notes", warning])
    return RecipeExport("\n".join(rows), "text/plain; charset=utf-8", "txt")
