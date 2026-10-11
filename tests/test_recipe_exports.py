"""Immutable, exact food-logging exports from saved report meals only."""

import json
from copy import deepcopy
from dataclasses import FrozenInstanceError
from decimal import Decimal
from typing import Any

import pytest

from grocery_agent.meals.catalog import Nutrients
from grocery_agent.meals.planner import IngredientPrice, MealLine, PlannedMeal
from grocery_agent.models.offer import Offer
from grocery_agent.recipe_exports import RecipeExport, export_recipe


@pytest.fixture
def saved_result(candidate: dict[str, Any]) -> dict[str, Any]:
    offer_data = deepcopy(candidate)
    offer_data["source_url"] = "https://private.invalid/catalogue?token=secret-credential"
    offer = Offer.model_validate(offer_data)
    lines = []
    for key, label, raw, edible, owned, purchased in (
        ("carrot", "Saved carrot", "300", "270", "300", "0"),
        ("chicken", "Saved chicken", "450", "450", "150", "300"),
        ("oil", "Saved oil", "15.000001", "15.000001", "15.000001", "0"),
        ("zero", "Zero food", "0", "0", "0", "0"),
    ):
        lines.append(
            MealLine(
                price=IngredientPrice(
                    ingredient_id=key,
                    label=label,
                    price_per_kg_czk="10",
                    offer=offer if key == "chicken" else None,
                ),
                required_grams=raw,
                edible_grams=edible,
                owned_grams=owned,
                purchased_grams=purchased,
                usage_cost_czk="5",
            )
        )
    meal = PlannedMeal(
        title="Saved bowl",
        minutes=25,
        servings=3,
        lines=tuple(lines),
        nutrients_per_serving=Nutrients(
            kcal="123.456789", protein_g="12.345678", carbs_g="34.567891", fat_g="5.123456"
        ),
        usage_cost_per_serving_czk="19",
        protein_g_per_czk=None,
        use_first_grams="300",
        stores=("private-store",),
        steps=("Cook thoroughly.", "Serve."),
    )
    return {
        "report": {
            "meals": [meal.model_dump(mode="json")],
            "pantry": {"private-item": "secret-credential"},
            "warnings": ["private account https://private.invalid/"],
        },
        "ingredient_snapshot": {
            "carrot": {"labels": {"cs": "Mrkev", "en": "Carrot"}},
            "chicken": {"labels": {"cs": "Kuře", "en": "Chicken"}},
            "oil": {"labels": {"cs": "Olej", "en": "Oil"}},
        },
        "provider": "private-provider",
        "account": "private-account",
        "fee_czk": "987654321.001",
        "request": {"pantry": {"private-item": "secret-credential"}},
        "warnings": ["secret-credential"],
    }


def test_export_dataclass_is_frozen_and_formats_are_backend_ready(saved_result: Any) -> None:
    export = export_recipe(saved_result, 0)
    assert isinstance(export, RecipeExport)
    assert (export.media_type, export.extension) == ("text/plain; charset=utf-8", "txt")
    with pytest.raises(FrozenInstanceError):
        export.body = "changed"
    ingredients = export_recipe(saved_result, 0, format="ingredients")
    assert (ingredients.media_type, ingredients.extension) == (export.media_type, "txt")
    schema = export_recipe(saved_result, 0, format="schema-org")
    assert (schema.media_type, schema.extension) == ("application/ld+json", "json")


def test_ingredients_use_full_recipe_edible_weights_including_owned_food_and_oil(
    saved_result: Any,
) -> None:
    assert export_recipe(saved_result, 0, format="ingredients").body == (
        "270 g Mrkev\n450 g Kuře\n15.000001 g Olej"
    )
    assert export_recipe(saved_result, 0, format="ingredients", language="en").body == (
        "270 g Carrot\n450 g Chicken\n15.000001 g Oil"
    )


@pytest.mark.parametrize("language", ["cs", "en"])
def test_full_text_includes_trimming_servings_steps_and_exact_whole_nutrition(
    saved_result: Any, language: str
) -> None:
    text = export_recipe(saved_result, 0, language=language).body
    assert "Saved bowl" in text and "300 g" in text and "270 g" in text
    assert "15.000001 g" in text
    assert ("Počet porcí: 3" if language == "cs" else "Servings: 3") in text
    assert "1. Cook thoroughly.\n2. Serve." in text
    for value in (
        "123.456789 kcal",
        "370.370367 kcal",
        "37.037034 g",
        "103.703673 g",
        "15.370368 g",
    ):
        assert value in text
    for label in (
        ("Bílkoviny", "Sacharidy", "Tuky")
        if language == "cs"
        else ("Protein", "Carbohydrates", "Fat")
    ):
        assert label in text
    assert (
        "Hmotnost hotového jídla není známa" if language == "cs" else "Cooked yield is unknown"
    ) in text
    assert (
        "na 100 g nelze určit" if language == "cs" else "per 100 g cannot be determined"
    ) in text
    assert (
        "koření bez uvedeného množství"
        if language == "cs"
        else "condiments without recorded amounts"
    ) in text


def test_schema_org_has_raw_edible_ingredients_and_only_per_serving_nutrition(
    saved_result: Any,
) -> None:
    document = json.loads(export_recipe(saved_result, 0, format="schema-org", language="en").body)
    assert document["@context"] == "https://schema.org" and document["@type"] == "Recipe"
    assert document["name"] == "Saved bowl" and document["recipeYield"] == "3"
    assert document["totalTime"] == "PT25M"
    assert document["recipeIngredient"] == ["270 g Carrot", "450 g Chicken", "15.000001 g Oil"]
    assert document["recipeInstructions"] == [
        {"@type": "HowToStep", "text": "Cook thoroughly."},
        {"@type": "HowToStep", "text": "Serve."},
    ]
    assert document["nutrition"] == {
        "@type": "NutritionInformation",
        "servingSize": "1 serving",
        "calories": "123.456789 kcal",
        "proteinContent": "12.345678 g",
        "carbohydrateContent": "34.567891 g",
        "fatContent": "5.123456 g",
    }
    assert "raw edible" in document["description"]
    assert "Cooked yield is unknown" in document["description"]
    assert "condiments" in document["description"]


@pytest.mark.parametrize("format", ["text", "ingredients", "schema-org"])
def test_exports_do_not_mutate_or_leak_private_metadata(saved_result: Any, format: str) -> None:
    original = deepcopy(saved_result)
    body = export_recipe(saved_result, 0, format=format).body
    assert saved_result == original
    for private in (
        "private.invalid",
        "secret-credential",
        "private-provider",
        "private-account",
        "private-store",
        "private-item",
        "987654321.001",
        "CZK",
        "usage_cost_czk",
        "fee_czk",
        "source_url",
    ):
        assert private not in body


def test_selected_meal_only_and_malformed_unselected_meal_is_ignored(saved_result: Any) -> None:
    meal = deepcopy(saved_result["report"]["meals"][0])
    meal["title"] = "Second meal"
    saved_result["report"]["meals"] = [{"legacy": "unrelated malformed meal"}, meal]
    body = export_recipe(saved_result, 1).body
    assert "Second meal" in body and "Saved bowl" not in body
    with pytest.raises(ValueError):
        export_recipe(saved_result, 0)


@pytest.mark.parametrize("index", [-1, 1, 100, True, 0.0, "0", None])
def test_invalid_selection_bounds_and_types(saved_result: Any, index: Any) -> None:
    with pytest.raises(ValueError):
        export_recipe(saved_result, index)


@pytest.mark.parametrize(
    "result",
    [
        None,
        {},
        {"report": None},
        {"report": {}},
        {"report": {"meals": []}},
        {"report": {"meals": {"0": {}}}},
        {"report": {"meals": [{"name": "Old placeholder"}]}},
    ],
)
def test_missing_or_malformed_legacy_results_refuse_export(result: Any) -> None:
    with pytest.raises(ValueError):
        export_recipe(result, 0)


@pytest.mark.parametrize(
    "field", ["required_grams", "edible_grams", "owned_grams", "purchased_grams"]
)
@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "-1", 1.25, True])
def test_bad_legacy_quantities_are_rejected(saved_result: Any, field: str, value: Any) -> None:
    saved_result["report"]["meals"][0]["lines"][0][field] = value
    with pytest.raises(ValueError):
        export_recipe(saved_result, 0)


def test_edible_cannot_exceed_raw_and_missing_lines_are_rejected(saved_result: Any) -> None:
    meal = saved_result["report"]["meals"][0]
    meal["lines"][0]["edible_grams"] = "301"
    with pytest.raises(ValueError, match="edible weight"):
        export_recipe(saved_result, 0)
    meal["lines"] = []
    with pytest.raises(ValueError):
        export_recipe(saved_result, 0)


@pytest.mark.parametrize(
    "field,value",
    [
        ("servings", 0),
        ("servings", -1),
        ("minutes", -1),
        ("servings", 1.25),
        ("minutes", 1.25),
        ("servings", True),
        ("minutes", False),
    ],
)
def test_invalid_servings_and_minutes(saved_result: Any, field: str, value: Any) -> None:
    saved_result["report"]["meals"][0][field] = value
    with pytest.raises(ValueError):
        export_recipe(saved_result, 0)


def test_zero_minutes_is_valid(saved_result: Any) -> None:
    saved_result["report"]["meals"][0]["minutes"] = 0
    document = json.loads(export_recipe(saved_result, 0, format="schema-org").body)
    assert document["totalTime"] == "PT0M"


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "-1", 1.25])
def test_invalid_nutrition_is_rejected(saved_result: Any, value: Any) -> None:
    saved_result["report"]["meals"][0]["nutrients_per_serving"]["kcal"] = value
    with pytest.raises(ValueError):
        export_recipe(saved_result, 0)


@pytest.mark.parametrize(
    "snapshot", [None, {}, {"carrot": None}, {"carrot": {"labels": {"en": ""}}}]
)
def test_legacy_snapshot_falls_back_to_saved_price_labels(saved_result: Any, snapshot: Any) -> None:
    saved_result["ingredient_snapshot"] = snapshot
    body = export_recipe(saved_result, 0, format="ingredients", language="en").body
    assert body == "270 g Saved carrot\n450 g Saved chicken\n15.000001 g Saved oil"


def test_decimal_strings_never_use_scientific_notation_or_round_large_totals(
    saved_result: Any,
) -> None:
    meal = saved_result["report"]["meals"][0]
    meal["lines"][2]["required_grams"] = "0.00000001"
    meal["lines"][2]["edible_grams"] = "0.00000001"
    meal["lines"][2]["owned_grams"] = "0.00000001"
    meal["nutrients_per_serving"]["kcal"] = "123456789012345678901234567890.123456"
    text = export_recipe(saved_result, 0, language="en").body
    assert "0.00000001 g Oil" in text
    assert "370370367037037036703703703670.370368 kcal" in text
    assert "E+" not in text and "E-" not in text
    assert Decimal("123.456789") * 3 == Decimal("370.370367")


def test_malicious_saved_strings_remain_valid_json_and_one_ingredient_line(
    saved_result: Any,
) -> None:
    meal = saved_result["report"]["meals"][0]
    title = 'Recipe " } </script><script>alert("x")</script>\nnext'
    step = 'Mix "\\".\n<script>example</script>'
    meal["title"] = title
    meal["steps"] = [step]
    saved_result["ingredient_snapshot"]["carrot"]["labels"]["en"] = 'Carrot\n"quoted"'
    document = json.loads(export_recipe(saved_result, 0, format="schema-org", language="en").body)
    assert document["name"] == title
    assert document["recipeInstructions"][0]["text"] == step
    assert document["recipeIngredient"][0] == '270 g Carrot "quoted"'
    assert (
        len(export_recipe(saved_result, 0, format="ingredients", language="en").body.splitlines())
        == 3
    )


@pytest.mark.parametrize("options", [{"format": "xml"}, {"language": "de"}])
def test_unknown_export_options_rejected(saved_result: Any, options: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        export_recipe(saved_result, 0, **options)


@pytest.mark.parametrize("line", [None, {}, {"price": {}}, {"unexpected": "secret-credential"}])
def test_malformed_lines_raise_safe_value_error(saved_result: Any, line: Any) -> None:
    saved_result["report"]["meals"][0]["lines"] = [line]
    with pytest.raises(ValueError) as error:
        export_recipe(saved_result, 0)
    assert "secret-credential" not in str(error.value)


def test_legacy_labels_do_not_reload_current_config(
    saved_result: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from grocery_agent.meals.catalog import MealCatalog

    def forbidden(*args: Any) -> Any:
        raise AssertionError("exports must never load current configuration")

    monkeypatch.setattr(MealCatalog, "load", forbidden)
    saved_result.pop("ingredient_snapshot")
    assert "Saved carrot" in export_recipe(saved_result, 0, format="ingredients").body


def test_production_report_json_with_computed_offers_exports_without_mutation(
    candidate: Any,
) -> None:
    from grocery_agent.meals.catalog import MealCatalog
    from grocery_agent.meals.planner import plan_meals
    from tests.test_meals import NOW, ROOT, MemoryReader, groceries

    catalog = MealCatalog.load(ROOT / "config/meals.toml")
    report = plan_meals(MemoryReader(groceries(candidate)), catalog, NOW)
    result = {"report": report.model_dump(mode="json")}
    original = deepcopy(result)
    document = json.loads(export_recipe(result, 0, format="schema-org").body)
    assert document["name"] == report.meals[0].title
    assert document["recipeYield"] == str(report.meals[0].servings)
    assert len(document["recipeIngredient"]) == len(report.meals[0].lines)
    assert result == original


@pytest.mark.parametrize(
    "language,saved_label,expected",
    [
        ("en", "Carrots (raw, before trimming)", "Carrots (raw)"),
        ("en", "Onion (raw, before peeling)", "Onion (raw)"),
        ("cs", "Mrkev (syrová, před očištěním)", "Mrkev (syrová)"),
        ("cs", "Cibule (syrová, před oloupáním)", "Cibule (syrová)"),
    ],
)
@pytest.mark.parametrize("frozen_labels", [False, True])
@pytest.mark.parametrize("format", ["text", "ingredients", "schema-org"])
def test_edible_exports_remove_known_raw_trimming_qualifiers_without_mutation(
    saved_result: Any,
    language: str,
    saved_label: str,
    expected: str,
    frozen_labels: bool,
    format: str,
) -> None:
    if frozen_labels:
        saved_result["ingredient_snapshot"]["carrot"]["labels"][language] = saved_label
    else:
        saved_result.pop("ingredient_snapshot")
        saved_result["report"]["meals"][0]["lines"][0]["price"]["label"] = saved_label
    original = deepcopy(saved_result)
    body = export_recipe(saved_result, 0, language=language, format=format).body
    assert f"270 g {expected}" in body
    assert saved_label not in body
    if format == "schema-org":
        assert json.loads(body)["recipeIngredient"][0] == f"270 g {expected}"
    elif format == "text":
        assert "300 g" in body
    assert saved_result == original


@pytest.mark.parametrize(
    "label",
    [
        "Carrots, before trimming",
        "Carrots (cooked, before trimming)",
        "Carrots (raw, before washing)",
        "Carrots (raw,before trimming)",
        "Mrkev, před očištěním",
        "Mrkev (vařená, před očištěním)",
        "Mrkev (syrová, před vařením)",
    ],
)
def test_unrelated_saved_label_qualifiers_are_preserved(saved_result: Any, label: str) -> None:
    saved_result.pop("ingredient_snapshot")
    saved_result["report"]["meals"][0]["lines"][0]["price"]["label"] = label
    assert (
        export_recipe(saved_result, 0, format="ingredients").body.splitlines()[0]
        == f"270 g {label}"
    )
