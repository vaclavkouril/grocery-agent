import json
import subprocess
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from bs4 import BeautifulSoup
from pydantic import ValidationError
from sqlalchemy import select

from grocery_agent.apps import recipes as cli
from grocery_agent.channel_commands import parse_channel_command
from grocery_agent.config import Settings
from grocery_agent.localization import channel_help, format_decimal, localized_notice
from grocery_agent.meals.catalog import MealCatalog
from grocery_agent.meals.planner import plan_meals
from grocery_agent.meals.report import render_html
from grocery_agent.persistence.control.schema import ChannelConfirmationRow
from grocery_agent.recipe_config import RecipeSettings
from grocery_agent.recipe_service import RecipeService
from grocery_agent.recipes import (
    CodexProvider,
    OllamaProvider,
    RecipeRequest,
    TemplateProvider,
    recipe_prompt,
)
from tests.test_channels_backend import capabilities, intake, verify
from tests.test_meals import NOW, MemoryReader, groceries

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def channels(tmp_path: Path) -> Any:
    from grocery_agent.backend.channels import ChannelRepository
    from grocery_agent.backend.repository import ControlRepository
    from grocery_agent.persistence.database import create_database_engine
    from grocery_agent.persistence.migrations import upgrade_database

    engine = create_database_engine(f"sqlite:///{tmp_path / 'channels.db'}")
    upgrade_database(engine, "control")
    control = ControlRepository(engine)
    user = control.authenticate(control.bootstrap("admin", NOW), NOW).user_id
    yield ChannelRepository(control), user
    engine.dispose()


@pytest.fixture
def catalog() -> MealCatalog:
    return MealCatalog.load(ROOT / "config/meals.toml")


@pytest.mark.parametrize("language", ["cs", "en"])
def test_provider_prompt_language_and_canonical_ids(language: str) -> None:
    request = RecipeRequest(language=language, max_cost_per_serving_czk="12.3400")
    prompt = json.loads(recipe_prompt(request, '{"rice": {}}'))
    assert prompt["language"] == language
    assert ("Czech" if language == "cs" else "English") in prompt["instructions"]
    assert prompt["request"]["max_cost_per_serving_czk"] == "12.3400"
    assert "canonical ingredient IDs unchanged" in prompt["instructions"]
    assert prompt["context"] == '{"rice": {}}'
    assert TemplateProvider().generate(request, "")["title"] == (
        "Jednoduchá pánev" if language == "cs" else "Simple skillet"
    )


def test_legacy_request_and_catalog_fallback(catalog: MealCatalog) -> None:
    request = RecipeRequest.model_validate({"servings": 1})
    assert request.language == "en" and "language" not in request.model_fields_set
    payload = catalog.model_dump()
    for ingredient in payload["ingredients"].values():
        ingredient.pop("labels")
    for recipe in payload["recipes"]:
        recipe.pop("titles")
        recipe.pop("translated_steps")
    legacy = MealCatalog.model_validate(payload)
    assert legacy.ingredients["rice"].localized_label("cs") == legacy.ingredients["rice"].label
    assert legacy.recipes[0].localized("cs").steps == legacy.recipes[0].steps
    with pytest.raises(ValidationError):
        RecipeRequest(language="de")


@pytest.mark.parametrize("provider", ["codex", "ollama"])
def test_provider_adapters_send_requested_language(provider: str, monkeypatch: Any) -> None:
    request = RecipeRequest(language="cs", provider=provider, model="fixture")
    draft = {
        "title": "Rýže",
        "ingredients": [{"ingredient": "rice", "quantity": "100"}],
        "steps": ["Uvařte rýži."],
        "minutes": 20,
    }
    prompts = []

    def run(command: list[str], **kwargs: Any) -> Any:
        prompts.append(json.loads(kwargs["input"]))
        return subprocess.CompletedProcess(command, 0, json.dumps(draft), "")

    def post(url: str, **kwargs: Any) -> Any:
        import httpx

        prompts.append(json.loads(kwargs["json"]["prompt"]))
        return httpx.Response(
            200, request=httpx.Request("POST", url), json={"response": json.dumps(draft)}
        )

    monkeypatch.setattr("grocery_agent.recipes.subprocess.run", run)
    monkeypatch.setattr("grocery_agent.recipes.httpx.post", post)
    adapter = CodexProvider("fixture") if provider == "codex" else OllamaProvider("fixture")
    assert adapter.generate(request, '{"rice": {}}') == draft
    assert prompts[0]["language"] == prompts[0]["request"]["language"] == "cs"


def test_seed_has_both_languages(catalog: MealCatalog) -> None:
    assert all(set(item.labels) == {"cs", "en"} for item in catalog.ingredients.values())
    for recipe in catalog.recipes:
        assert set(recipe.titles) == set(recipe.translated_steps) == {"cs", "en"}
        assert recipe.translated_steps["en"] == recipe.steps
        assert len(recipe.translated_steps["cs"]) == len(recipe.steps)


def test_notice_localization_preserves_external_evidence() -> None:
    assert localized_notice("Acquisition coverage is unknown.", "cs") == (
        "Rozsah získaných dat není znám."
    )
    assert "Obnovení zdroje kupi selhalo" in localized_notice(
        "Refresh failed for kupi; using its fresh previous profile collection.", "cs"
    )
    unknown = 'External evidence {"raw": "<script>"}'
    assert localized_notice(unknown, "cs") == unknown


def test_exact_decimal_display() -> None:
    value = Decimal("12345678901234567890.340001")
    assert format_decimal(value, "cs") == "12345678901234567890,340001"
    assert format_decimal(value, "en") == "12345678901234567890.340001"
    assert format_decimal(Decimal("12.3400"), "cs", ".2f") == "12,34"
    assert str(value) == "12345678901234567890.340001"


def test_report_localized_numbers_dates_and_print(
    catalog: MealCatalog, candidate: dict[str, Any]
) -> None:
    report = plan_meals(MemoryReader(groceries(candidate)), catalog, NOW)
    before = report.model_dump_json()
    cs = BeautifulSoup(render_html(report, catalog, "cs"), "html.parser")
    en = BeautifulSoup(render_html(report, catalog), "html.parser")
    value = report.meals[0].usage_cost_per_serving_czk
    assert format_decimal(value, "cs", ".2f") + " Kč" in cs.get_text()
    assert format_decimal(value, "en", ".2f") + " Kč" in en.get_text()
    assert "01.10.2026" in cs.get_text() and "2026-10-01" in en.get_text()
    assert "day!=='2026-10-01'" in cs.find("script").text
    assert "@media print" in cs.find("style").text
    assert "table-header-group" in cs.find("style").text
    assert report.model_dump_json() == before


def test_report_localizes_ui_and_escapes_translations(
    catalog: MealCatalog, candidate: dict[str, Any]
) -> None:
    report = plan_meals(MemoryReader(groceries(candidate)), catalog, NOW)
    payload = catalog.model_dump()
    for recipe in payload["recipes"]:
        recipe["titles"]["cs"] = '<script>alert("title")</script>'
        recipe["translated_steps"]["cs"] = ['<img src=x onerror="step">']
    for ingredient in payload["ingredients"].values():
        ingredient["labels"]["cs"] = '<img src=x onerror="label">'
    unsafe = MealCatalog.model_validate(payload)
    before = report.model_dump_json()
    rendered = render_html(report, unsafe, "cs")
    soup = BeautifulSoup(rendered, "html.parser")
    assert soup.html["lang"] == "cs"
    assert soup.find("h3").text == "Postup"
    assert soup.find("th").text == "Surovina"
    assert soup.find("h2").text == '<script>alert("title")</script>'
    assert soup.find("img") is None
    assert len(soup.find_all("script")) == 1
    assert "Tento report je zastaral" in soup.find("script").text
    assert "protein / serving" not in rendered
    assert report.model_dump_json() == before
    assert 'lang="en"' in render_html(report, catalog)
    assert "Cook it" in render_html(report, catalog)


def pin(catalog: MealCatalog, candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": "run",
        "source_id": "kupi",
        "finished_at": NOW.isoformat(),
        "warnings": [],
        "latest_run_status": "complete",
        "source_scopes": {"kupi": (catalog.policy.scope,)},
        "offers": [
            {
                "offer": row.offer.model_dump(mode="json", exclude_computed_fields=True),
                "observed_at": NOW.isoformat(),
                "observation_id": None,
            }
            for row in groceries(candidate)
        ],
    }


def service(catalog: MealCatalog, candidate: dict[str, Any], monkeypatch: Any) -> RecipeService:
    result = RecipeService(Settings(), RecipeSettings(), clock=lambda: NOW)
    monkeypatch.setattr(MealCatalog, "load", lambda path: catalog)
    monkeypatch.setattr(result.cache, "pin", lambda *args: pin(catalog, candidate))
    return result


def test_language_pinned_templates_and_exact_costs(
    catalog: MealCatalog, candidate: dict[str, Any], monkeypatch: Any
) -> None:
    executor = service(catalog, candidate, monkeypatch)
    results = []
    for language in ("cs", "en"):
        request = RecipeRequest(provider="template", language=language)
        inputs = executor.prepare(request)
        assert inputs["prompt_version"] == "recipe-draft-v2"
        assert inputs["language"] == inputs["effective_request"]["language"] == language
        result, html = executor.execute(request, inputs)
        assert result["status"] == "ok" and f'lang="{language}"' in html
        assert result["report"]["meals"][0]["title"] in {
            item.titles[language] for item in catalog.recipes
        }
        results.append(result["report"]["meals"][0])
        with pytest.raises(ValueError, match="language does not match"):
            executor.execute(
                request.model_copy(update={"language": "en" if language == "cs" else "cs"}), inputs
            )
        legacy = {key: value for key, value in inputs.items() if key != "language"}
        legacy["prompt_version"] = "recipe-draft-v1"
        assert executor.execute(request, legacy)[0]["prompt_version"] == "recipe-draft-v1"
    assert results[0]["usage_cost_per_serving_czk"] == results[1]["usage_cost_per_serving_czk"]
    assert results[0]["nutrients_per_serving"] == results[1]["nutrients_per_serving"]


def test_no_feasible_localized_reason_supplements_original(
    catalog: MealCatalog, candidate: dict[str, Any], monkeypatch: Any
) -> None:
    executor = service(catalog, candidate, monkeypatch)
    request = RecipeRequest(provider="template", language="cs", max_minutes=1)
    result, html = executor.run(request)
    assert result["status"] == "no-feasible-recipe"
    assert result["reason"].startswith("no complete recipe")
    assert result["reason_code"] == "no-complete-recipe-within-limits"
    assert result["reason_localized"].startswith("Žádný úplný recept")
    assert 'lang="cs"' in html and "Žádný vyhovující recept" in html
    excluded = RecipeRequest(provider="template", language="cs", exclusions=("chicken", "turkey"))
    result, html = executor.run(excluded)
    assert result["reason_code"] == "no-template-after-exclusions"
    assert result["reason"] == "No configured template remains after exclusions."
    assert "Po vyloučení surovin" in html


def test_cli_and_channel_language(catalog: MealCatalog, monkeypatch: Any, tmp_path: Path) -> None:
    seen = []

    class Service:
        def __init__(self, *args: Any) -> None:
            pass

        def prepare(self, request: RecipeRequest) -> dict[str, Any]:
            seen.append(request)
            return {}

        def execute(self, *args: Any) -> tuple[dict[str, Any], str]:
            return {"status": "ok"}, "html"

    monkeypatch.setattr(cli, "RecipeService", Service)
    monkeypatch.setattr(cli, "Settings", lambda: Settings(report_dir=tmp_path, _env_file=None))
    assert cli.main(["--provider", "template", "--language", "cs", "--budget", "12.3400"]) == 0
    assert seen[0].language == "cs"
    assert str(seen[0].max_cost_per_serving_czk) == "12.3400"
    request = parse_channel_command("recipe language=cs budget=12.3400", capabilities())
    assert isinstance(request, RecipeRequest) and request.language == "cs"
    assert "language" in request.model_fields_set
    default = parse_channel_command("recipe", capabilities())
    assert isinstance(default, RecipeRequest) and "language" not in default.model_fields_set
    with pytest.raises(ValidationError):
        parse_channel_command("recipe language=de", capabilities())
    assert "language (cs/en)" in channel_help("cs")


def test_channels_account_preference_explicit_override_and_delivery(channels: Any) -> None:
    repo, user = channels
    verify(repo, user)
    from grocery_agent.account_models import SettingsPatch
    from grocery_agent.backend.user_state import UserStateRepository

    UserStateRepository(repo.control).patch_settings(
        user, SettingsPatch(expected_revision=0, ui_language="cs", recipe_language="cs")
    )
    assert intake(repo, "help-cs", "help")["reply"].startswith("Příkazy")
    confirmation = intake(repo, "recipe-cs", "recipe")
    assert confirmation["reply"].startswith("Parametry receptu")
    token = confirmation["reply"].split("confirm ")[1].split()[0].rstrip(".")
    accepted = intake(repo, "confirm-cs", f"confirm {token}")
    assert "přijata" in accepted["reply"]
    with repo.sessions() as session:
        saved = session.scalar(select(ChannelConfirmationRow))
        assert saved.request["language"] == "cs"
        job_id = saved.job_id
    assert "ve frontě" in intake(repo, "status-cs", f"status {job_id}")["reply"]
    repo.control.cancel(user, job_id, NOW)
    deliveries = []
    while delivery := repo.claim("simplex", NOW, 30, 5):
        deliveries.append(delivery)
        repo.ack("simplex", delivery["delivery_id"], delivery["lease_token"], NOW, None, 5)
    terminal = next(item for item in deliveries if item["subject"] == "Grocery Agent recept")
    assert terminal["text"] == f"Úloha {job_id}: zrušena."
    assert terminal["report_html"] is None
    intake(repo, "recipe-en", "recipe language=en")
    with repo.sessions() as session:
        assert {
            row.request["language"] for row in session.scalars(select(ChannelConfirmationRow))
        } == {"cs", "en"}
    repo.settings_reader = lambda _: {"ui_language": "cs", "recipe_language": None}
    assert repo._languages(user) == ("cs", "cs")
    repo.settings_reader = lambda _: {"ui_language": None, "recipe_language": None}
    assert repo._languages(user) == ("en", "en")
