"""Offline cuisine contracts, provider guidance, snapshot replay and CLI grammar."""

import json
from copy import deepcopy
from typing import Any

import pytest
from pydantic import ValidationError

from grocery_agent.apps import recipes as cli
from grocery_agent.apps.recipes import save_reports
from grocery_agent.channel_commands import HELP_TEXT, parse_channel_command
from grocery_agent.cuisines import CUISINE_IDS, CUISINE_LABELS
from grocery_agent.http_client import recipe_submit_payload
from grocery_agent.meals.catalog import MealCatalog
from grocery_agent.recipe_config import RecipeSettings
from grocery_agent.recipe_service import effective_catalog
from grocery_agent.recipes import (
    RecipeRequest,
    TemplateProvider,
    draft_schema,
    generate_recipe,
    recipe_prompt,
)
from tests import test_channel_commands as channel_tests
from tests import test_recipe_batches as batch_tests
from tests.test_recipe_batches import SequenceProvider, chicken_draft

capabilities = channel_tests.capabilities
batch_service = batch_tests.batch_service

EXPECTED_IDS = (
    "greek",
    "chinese",
    "vietnamese",
    "indian",
    "mexican",
    "italian",
    "thai",
    "japanese",
    "korean",
    "french",
    "spanish",
    "mediterranean",
    "czech",
    "middle_eastern",
)


def test_cuisine_list_and_schema_are_bounded_to_shared_ids() -> None:
    assert CUISINE_IDS == EXPECTED_IDS
    assert set(CUISINE_LABELS) == set(EXPECTED_IDS)
    assert all(set(labels) == {"cs", "en"} for labels in CUISINE_LABELS.values())
    schema = RecipeRequest.model_json_schema()["properties"]["cuisine"]
    assert schema["anyOf"][0]["enum"] == list(EXPECTED_IDS)
    assert RecipeRequest().cuisine is None


@pytest.mark.parametrize("cuisine", EXPECTED_IDS)
def test_cuisine_request_roundtrip_and_api_payload(cuisine: str) -> None:
    request = RecipeRequest(cuisine=cuisine)
    assert RecipeRequest.model_validate_json(request.model_dump_json()) == request
    assert recipe_submit_payload(request)["cuisine"] == cuisine
    with pytest.raises(ValidationError, match="frozen"):
        request.cuisine = "italian"


@pytest.mark.parametrize("value", ["Greek", "unknown", "none", "greek,chinese", "", 1, True])
def test_unknown_or_unstructured_cuisine_is_rejected(value: Any) -> None:
    with pytest.raises(ValidationError):
        RecipeRequest(cuisine=value)


@pytest.mark.parametrize("language", ["en", "cs"])
@pytest.mark.parametrize("cuisine", EXPECTED_IDS)
def test_prompt_uses_only_selected_cuisine_and_known_ingredients(
    language: str, cuisine: str
) -> None:
    request = RecipeRequest(cuisine=cuisine, language=language, condiments=("salt",))
    context = json.dumps({"chicken": {"label": "Chicken", "owned_stock": {"grams": "700"}}})
    prompt = json.loads(recipe_prompt(request, context))
    instructions = prompt["instructions"]
    assert CUISINE_LABELS[cuisine][language] in instructions
    assert f"({cuisine})" in instructions
    assert "known ingredient IDs with nutrition data in context" in instructions
    assert "explicitly selected condiments" in instructions
    assert "local adaptation" in instructions and "do not guarantee authenticity" in instructions
    assert prompt["context"] == context
    assert prompt["request"]["cuisine"] == cuisine
    assert draft_schema(context)["$defs"]["RecipeIngredient"]["properties"]["ingredient"][
        "enum"
    ] == ["chicken"]
    baseline = json.loads(
        recipe_prompt(RecipeRequest(language=language, condiments=("salt",)), context)
    )
    assert "selected cuisine" not in baseline["instructions"]
    assert len(instructions) - len(baseline["instructions"]) < 500


@pytest.mark.parametrize("count", [None, 2])
def test_cuisine_is_pinned_saved_and_replayed_without_mutating_inputs(
    batch_service: Any, count: int | None
) -> None:
    service, pins = batch_service
    request = RecipeRequest(cuisine="vietnamese", recipe_count=count, condiments=("salt",))
    inputs = service.prepare(request)
    original = deepcopy(inputs)
    service.provider = SequenceProvider(chicken_draft(), chicken_draft("720"))
    result, html = service.execute(request, inputs)
    assert inputs == original
    assert inputs["effective_request"]["cuisine"] == "vietnamese"
    assert inputs["prompt_version"] == "recipe-draft-v3-cuisine"
    assert result["prompt_version"] == "recipe-draft-v3-cuisine"
    assert result["effective_request"]["cuisine"] == "vietnamese"
    save_reports(service.settings, request, result, html, inputs=inputs)
    directory = service.settings.report_dir / "requests" / str(request.request_id)
    saved_request = RecipeRequest.model_validate_json((directory / "request.json").read_text())
    saved_inputs = json.loads((directory / "inputs.json").read_text())
    assert saved_request == request
    service.provider = SequenceProvider(chicken_draft(), chicken_draft("720"))
    replay, replay_html = service.execute(saved_request, saved_inputs)
    assert replay == result and replay_html == html and pins == [request]
    with pytest.raises(ValueError, match="pinned recipe cuisine"):
        service.execute(request.model_copy(update={"cuisine": "greek"}), inputs)


@pytest.mark.parametrize("count", [None, 2])
def test_template_cuisine_is_rejected_before_cache_and_templates_stay_original(
    batch_service: Any, count: int | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, pins = batch_service
    base = MealCatalog.load(service.settings.meal_config)
    before = base.model_dump(mode="json")
    request = RecipeRequest(provider="template", recipe_count=count, cuisine="greek")

    def forbidden(*args: Any) -> Any:
        raise AssertionError("invalid template cuisine must not access the cache")

    monkeypatch.setattr(service.cache, "scopes", forbidden)
    monkeypatch.setattr(service.cache, "pin", forbidden)
    with pytest.raises(ValueError, match="choose provider=codex or provider=ollama"):
        effective_catalog(base, request)
    with pytest.raises(ValueError, match="cuisine=none"):
        service.prepare(request)
    with pytest.raises(ValueError, match="cuisine=none"):
        service.execute(request, {})
    assert pins == []
    assert base.model_dump(mode="json") == before
    assert effective_catalog(base, RecipeRequest(provider="template")).recipes == base.recipes


def test_no_cuisine_preserves_legacy_prompt_version(batch_service: Any) -> None:
    service, _ = batch_service
    assert service.prepare(RecipeRequest())["prompt_version"] == "recipe-draft-v2"


def test_legacy_absent_cuisine_allows_provider_swap_with_none(batch_service: Any) -> None:
    service, _ = batch_service
    request = RecipeRequest()
    inputs = service.prepare(request)
    inputs["effective_request"].pop("cuisine")
    result, _ = service.execute(request.model_copy(update={"provider": "template"}), inputs)
    assert result["status"] == "ok"
    assert result["prompt_version"] == "recipe-draft-v2"
    with pytest.raises(ValueError, match="pinned recipe cuisine"):
        service.execute(request.model_copy(update={"cuisine": "greek"}), inputs)


def test_cuisine_preserves_decimal_planning_and_accepts_pinned_prompt_version(
    batch_service: Any,
) -> None:
    service, _ = batch_service
    request = RecipeRequest(cuisine="greek", condiments=("salt",))
    inputs = service.prepare(request)
    inputs["prompt_version"] = "recipe-draft-v2"
    service.provider = SequenceProvider(chicken_draft())
    result, _ = service.execute(request, inputs)
    assert result["prompt_version"] == "recipe-draft-v2"
    service.provider = SequenceProvider(chicken_draft())
    baseline, _ = service.run(RecipeRequest(condiments=("salt",)))
    assert result["report"] == baseline["report"]


@pytest.mark.parametrize("cuisine", EXPECTED_IDS)
def test_channel_cuisine_ids_and_explicit_none(capabilities: Any, cuisine: str) -> None:
    request = parse_channel_command(f"recipe provider=codex cuisine={cuisine}", capabilities)
    assert request.cuisine == cuisine
    assert cuisine in HELP_TEXT
    cleared = parse_channel_command("recipe cuisine=none", capabilities)
    assert cleared.cuisine is None and "cuisine" in cleared.model_fields_set
    assert "cuisine" not in parse_channel_command("recipe", capabilities).model_fields_set


@pytest.mark.parametrize(
    "text", ["cuisine=Greek", "cuisine=unknown", "cuisine=greek cuisine=chinese"]
)
def test_channel_invalid_cuisine_grammar(capabilities: Any, text: str) -> None:
    with pytest.raises(ValueError):
        parse_channel_command("recipe provider=codex " + text, capabilities)


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("cuisine", [None, *EXPECTED_IDS])
def test_cli_cuisine_parity_and_omission(
    monkeypatch: pytest.MonkeyPatch, capabilities: Any, remote: bool, cuisine: str | None
) -> None:
    from types import SimpleNamespace

    requests: list[RecipeRequest] = []

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
            requests.append(request)
            return SimpleNamespace(model_dump_json=lambda: "{}")

        def prepare(self, request: RecipeRequest) -> dict[str, Any]:
            requests.append(request)
            return {}

        def execute(self, *args: Any) -> tuple[dict[str, Any], str]:
            return {}, ""

    monkeypatch.setenv("GROCERY_API_TOKEN", "offline-token")
    monkeypatch.setattr(cli, "GroceryClient", Client)
    monkeypatch.setattr(cli, "RecipeService", Client)
    monkeypatch.setattr(cli, "save_reports", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli.RecipeSettings, "load", lambda path: RecipeSettings())
    args = ["--provider", "codex"]
    if remote:
        args += ["--api-url", "http://offline.invalid", "--no-wait"]
    if cuisine is not None:
        args += ["--cuisine", cuisine]
    assert cli.main(args) == 0
    request = requests[0]
    assert request.cuisine == cuisine
    assert ("cuisine" in request.model_fields_set) is (cuisine is not None)
    payload = recipe_submit_payload(request)
    assert payload.get("cuisine") == cuisine
    assert ("cuisine" in payload) is (cuisine is not None)


@pytest.mark.parametrize("value", ["unknown", "Greek", "none"])
def test_cli_unknown_cuisine_rejected_before_service(value: str) -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(["--cuisine", value])
    assert error.value.code == 2


@pytest.mark.parametrize("injected", [False, True])
def test_generate_recipe_rejects_template_cuisine_before_provider_call(
    monkeypatch: pytest.MonkeyPatch, injected: bool
) -> None:
    calls: list[RecipeRequest] = []

    class Provider:
        def generate(self, request: RecipeRequest, context: str) -> dict[str, Any]:
            calls.append(request)
            return chicken_draft()

    def forbidden(*args: Any) -> Any:
        raise AssertionError("template provider must not be invoked for a selected cuisine")

    monkeypatch.setattr(TemplateProvider, "generate", forbidden)
    with pytest.raises(ValueError, match="choose provider=codex or provider=ollama"):
        generate_recipe(
            RecipeRequest(provider="template", cuisine="greek"),
            provider=Provider() if injected else None,
        )
    assert calls == []


@pytest.mark.parametrize("provider", ["template", "codex"])
def test_direct_template_provider_rejects_cuisine(provider: str) -> None:
    with pytest.raises(ValueError, match="cuisine=none"):
        TemplateProvider().generate(RecipeRequest(provider=provider, cuisine="greek"), "")


@pytest.mark.parametrize("language", ["cs", "en"])
def test_generate_recipe_legacy_template_and_injected_provider_unchanged(language: str) -> None:
    request = RecipeRequest(provider="template", language=language)
    draft = TemplateProvider().generate(request, "")
    result = generate_recipe(request)
    assert result.title == ("Jednoduchá pánev" if language == "cs" else "Simple skillet")
    assert result.steps == tuple(draft["steps"])
    assert result.ingredients[0].ingredient == "zelenina"
    assert result.ingredients[0].quantity == 300
    provider = SequenceProvider(draft)
    assert generate_recipe(request, provider=provider) == result
    assert len(provider.calls) == 1
