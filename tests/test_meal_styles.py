from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from grocery_agent.application.commands import MealCommand, parse_command
from grocery_agent.application.parameters import MealOverrides, PolicyOverrides, resolve_parameters
from grocery_agent.application.services import MealService
from grocery_agent.cli.main import build_parser, meal_overrides
from grocery_agent.meals.catalog import MealCatalog
from grocery_agent.meals.planner import plan_meals
from grocery_agent.meals.report import render_html
from tests.application_support import NOW
from tests.test_application import EmptyBatchReader, owned_all
from tests.test_meals import MemoryReader, groceries

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def catalog() -> MealCatalog:
    return MealCatalog.load(ROOT / "config/meals.toml")


@pytest.mark.parametrize(
    "style,protein,kcal,cost,count",
    [
        ("main", "70", "850", "100", 4),
        ("breakfast", "35", "650", "80", 2),
        ("snack", "25", "450", "60", 2),
    ],
)
def test_styles_resolve_defaults_and_filter_recipes(
    catalog: MealCatalog,
    style: str,
    protein: str,
    kcal: str,
    cost: str,
    count: int,
) -> None:
    overrides = owned_all(catalog).model_dump()
    overrides["policy"] = {"meal_style": style}
    command = MealCommand(overrides=MealOverrides.model_validate(overrides))
    execution = MealService(EmptyBatchReader(), catalog, lambda: NOW).plan(command)
    policy = execution.parameters.policy
    assert (policy.min_protein_g, policy.max_kcal, policy.max_cost_per_serving_czk) == (
        Decimal(protein),
        Decimal(kcal),
        Decimal(cost),
    )
    assert len(execution.report.meals) == count
    assert all(meal.meal_style == style for meal in execution.report.meals)
    assert all(
        meal.nutrients_per_serving.protein_g >= policy.min_protein_g
        and meal.nutrients_per_serving.kcal <= policy.max_kcal
        for meal in execution.report.meals
    )
    assert f"Meal style: {style}" in render_html(execution.report, execution.catalog_snapshot)


def test_explicit_limits_override_style_defaults_and_switch_back_to_main(
    catalog: MealCatalog,
) -> None:
    breakfast = resolve_parameters(
        catalog,
        MealOverrides(
            policy=PolicyOverrides(
                meal_style="breakfast", min_protein_g="40", max_cost_per_serving_czk="45"
            )
        ),
    )
    assert breakfast.policy.min_protein_g == 40 and breakfast.policy.max_cost_per_serving_czk == 45
    # A saved breakfast profile keeps its explicit limits on subsequent breakfast requests.
    inherited = resolve_parameters(catalog, MealOverrides(), breakfast)
    assert inherited == breakfast
    main = resolve_parameters(
        catalog, MealOverrides(policy=PolicyOverrides(meal_style="main")), breakfast
    )
    assert main.policy.min_protein_g == 70 and main.policy.max_kcal == 850
    assert catalog.policy.meal_style == "main" and len(catalog.recipes) == 8


@pytest.mark.parametrize("style", ["breakfast", "snack"])
def test_styles_use_fixture_prices_and_keep_lactose_free_pantry_rules(
    catalog: MealCatalog,
    candidate: dict[str, Any],
    style: str,
) -> None:
    args = build_parser().parse_args(
        [
            "meals",
            "--meal-style",
            style,
            "--have",
            "chicken=2kg",
            "--have",
            "oil=available",
            "--have-seasonings",
            "--use-first",
            "chicken",
        ]
    )
    parameters = resolve_parameters(catalog, meal_overrides(args))
    report = plan_meals(MemoryReader(groceries(candidate)), parameters.apply_to(catalog), NOW)
    assert report.policy.lactose_free and report.policy.meal_style == style
    for meal in report.meals:
        assert meal.meal_style == style
        for line in meal.lines:
            if line.price.ingredient_id in {"chicken", "oil"}:
                assert line.usage_cost_czk == 0
    text = parse_command(f"meal meal_style={style} have=chicken=2kg,oil=available")
    assert isinstance(text, MealCommand) and text.overrides.policy.meal_style == style


def test_unsupported_styles_are_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_command("meal meal_style=anything")
