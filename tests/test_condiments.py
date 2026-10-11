"""Explicit seasoning selection, named stock, and CLI/channel parity."""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from grocery_agent.apps import recipes as cli
from grocery_agent.channel_commands import parse_channel_command
from grocery_agent.meals.catalog import MealCatalog
from grocery_agent.recipe_config import RecipeSettings
from grocery_agent.recipe_service import effective_catalog
from grocery_agent.recipes import CONDIMENT_IDS, RecipeRequest, draft_schema, recipe_prompt
from tests import test_channel_commands as channel_tests
from tests import test_recipe_batches as batch_tests
from tests.test_recipe_batches import SequenceProvider, chicken_draft

capabilities = channel_tests.capabilities
batch_service = batch_tests.batch_service


def test_exact_seven_ids_and_roundtrip() -> None:
    assert CONDIMENT_IDS == (
        "salt",
        "black_pepper",
        "paprika",
        "dried_herbs",
        "cumin",
        "chilli",
        "garlic_powder",
    )
    request = RecipeRequest(condiments=CONDIMENT_IDS)
    assert RecipeRequest.model_validate_json(request.model_dump_json()) == request
    assert RecipeRequest().condiments is None and RecipeRequest(condiments=()).condiments == ()


@pytest.mark.parametrize("values", [("oil",), ("pepper",), ("salt", "salt"), ("",)])
def test_only_unique_canonical_condiments(values: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError):
        RecipeRequest(condiments=values)


@pytest.mark.parametrize("explicit", [(), ("salt",)])
@pytest.mark.parametrize("legacy", [True, False])
def test_explicit_selection_overrides_legacy_without_adding_oil(
    explicit: tuple[str, ...], legacy: bool
) -> None:
    base = MealCatalog.load(Path(__file__).resolve().parents[1] / "config/meals.toml")
    result = effective_catalog(
        base, RecipeRequest(condiments=explicit, seasonings_available=legacy)
    )
    assert result.pantry.seasonings_available is bool(explicit)
    assert result.pantry.items == base.pantry.items
    assert result.ingredients == base.ingredients
    assert (
        effective_catalog(
            base, RecipeRequest(seasonings_available=legacy)
        ).pantry.seasonings_available
        is legacy
    )


@pytest.mark.parametrize("count", [None, 2])
def test_condiment_warning_and_deterministic_nutrition(
    batch_service: Any, count: int | None
) -> None:
    service, _ = batch_service
    service.provider = SequenceProvider(chicken_draft(), chicken_draft("720"))
    result, html = service.run(RecipeRequest(recipe_count=count, condiments=("salt",)))
    assert any("nutrition are not included" in warning for warning in result["warnings"])
    assert "nutrition are not included" in html
    service.provider = SequenceProvider(chicken_draft(), chicken_draft("720"))
    baseline, _ = service.run(RecipeRequest(recipe_count=count, seasonings_available=True))
    assert result["report"]["meals"] == baseline["report"]["meals"]


def test_empty_selection_retains_estimated_allowance_with_warning(batch_service: Any) -> None:
    service, _ = batch_service
    service.provider = SequenceProvider(chicken_draft())
    result, html = service.run(RecipeRequest(condiments=(), seasonings_available=True))
    assert "estimated seasoning allowance" in html
    assert result["report"]["pantry"]["seasonings_available"] is False
    assert Decimal(result["report"]["meals"][0]["usage_cost_per_serving_czk"]) == 38


@pytest.mark.parametrize("count", [None, 2])
@pytest.mark.parametrize("condiments", [None, (), ("salt",)])
@pytest.mark.parametrize("language", ["en", "cs"])
def test_template_seasoning_warning_preserves_original_steps(
    batch_service: Any, count: int | None, condiments: tuple[str, ...] | None, language: str
) -> None:
    service, _ = batch_service
    request = RecipeRequest(
        provider="template", recipe_count=count, condiments=condiments, language=language
    )
    inputs = service.prepare(request)
    result, html = service.execute(request, inputs)
    assert result["status"] == "ok"
    expected = (
        "Nakonfigurované recepty zachovávají původní pokyny ke koření; "
        "vynechte koření, které jste nevybrali."
        if language == "cs"
        else "Configured recipes keep their original seasoning instructions; "
        "omit condiments you have not selected."
    )
    assert any(expected in warning for warning in result["warnings"]) is (condiments is not None)
    assert any(expected in warning for warning in result["report"]["warnings"]) is (
        condiments is not None
    )
    assert (expected in html) is (condiments is not None)
    templates = MealCatalog.model_validate(inputs["catalog"]).recipes
    for meal in result["report"]["meals"]:
        original = next(
            recipe.localized(language)
            for recipe in templates
            if recipe.localized(language).title == meal["title"]
        )
        assert tuple(meal["steps"]) == original.steps


def test_empty_template_inventory_warns_even_without_allowance(batch_service: Any) -> None:
    service, _ = batch_service
    request = RecipeRequest(provider="template", condiments=())
    inputs = service.prepare(request)
    inputs["catalog"]["policy"]["seasoning_allowance_czk"] = "0"
    result, html = service.execute(request, inputs)
    assert any("omit condiments you have not selected" in warning for warning in result["warnings"])
    assert "omit condiments you have not selected" in html


def test_named_stock_context_is_bounded_and_schema_stays_single_draft(batch_service: Any) -> None:
    service, _ = batch_service
    service.provider = SequenceProvider(chicken_draft())
    request = RecipeRequest(pantry={"chicken": "700g", "rice": "500g"}, use_first=("chicken",))
    service.backend = RecipeSettings(context_ingredients=1)
    inputs = service.prepare(request)
    service.execute(request, inputs)
    context = service.provider.calls[0][1]
    assert json.loads(context)["chicken"]["owned_stock"] == {"grams": "700", "use_first": True}
    assert '"rice"' not in context
    schema = draft_schema(context)
    assert schema["$defs"]["RecipeIngredient"]["properties"]["ingredient"]["enum"] == ["chicken"]
    prompt = json.loads(
        recipe_prompt(request.model_copy(update={"condiments": ("salt",)}), context)
    )
    assert "Never assume oil" in prompt["instructions"]
    assert "steps only" in prompt["instructions"]


def test_channel_count_and_condiments(capabilities: Any) -> None:
    request = parse_channel_command("recipe recipe_count=3 condiments=salt,cumin", capabilities)
    assert request.recipe_count == 3 and request.condiments == ("salt", "cumin")
    empty = parse_channel_command("recipe condiments=none have_seasonings=true", capabilities)
    assert empty.condiments == ()
    for text in ("recipe_count=7", "condiments=oil", "condiments=salt,salt"):
        with pytest.raises(ValueError):
            parse_channel_command("recipe " + text, capabilities)


def test_cli_count_and_repeated_condiment(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[RecipeRequest] = []

    class Service:
        def __init__(self, *args: Any) -> None:
            pass

        def prepare(self, request: RecipeRequest) -> dict[str, Any]:
            captured.append(request)
            return {}

        def execute(self, *args: Any) -> tuple[dict[str, Any], str]:
            return {}, ""

    monkeypatch.setattr(cli, "RecipeService", Service)
    monkeypatch.setattr(cli, "save_reports", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli.RecipeSettings, "load", lambda path: RecipeSettings())
    assert cli.main(["--recipe-count", "3", "--condiment", "salt", "--condiment", "cumin"]) == 0
    assert captured[0].recipe_count == 3 and captured[0].condiments == ("salt", "cumin")
    assert cli.main(["--condiment", "salt", "--condiment", "salt"]) == 1


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
def test_cli_shopping_omissions_preserve_account_defaults(
    monkeypatch: pytest.MonkeyPatch, capabilities: Any, remote: bool, explicit: bool
) -> None:
    from types import SimpleNamespace

    from grocery_agent.http_client import recipe_submit_payload

    captured: list[RecipeRequest] = []

    class Client:
        def __init__(self, *args: Any) -> None:
            pass

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def capabilities(self) -> Any:
            return capabilities

        def submit(self, request: RecipeRequest) -> Any:
            captured.append(request)
            return SimpleNamespace(model_dump_json=lambda: "{}")

        def prepare(self, request: RecipeRequest) -> dict[str, Any]:
            captured.append(request)
            return {}

        def execute(self, *args: Any) -> tuple[dict[str, Any], str]:
            return {}, ""

    monkeypatch.setenv("GROCERY_API_TOKEN", "fixture-token")
    monkeypatch.setattr(cli, "GroceryClient", Client)
    monkeypatch.setattr(cli, "RecipeService", Client)
    monkeypatch.setattr(cli, "save_reports", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli.RecipeSettings, "load", lambda path: RecipeSettings())
    args = ["--api-url", "http://offline.invalid", "--no-wait"] if remote else []
    if explicit:
        args += ["--max-stores", "1", "--retailer", "billa"]
    assert cli.main(args) == 0
    request = captured[0]
    assert request.max_stores == 1
    assert request.retailer_ids == (("billa",) if explicit else ())
    payload = recipe_submit_payload(request)
    for field in ("max_stores", "retailer_ids"):
        assert (field in request.model_fields_set) is explicit
        assert (field in payload) is explicit
