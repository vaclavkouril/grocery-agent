"""Offline batch generation against one pinned catalogue and offer snapshot."""

import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from grocery_agent.config import Settings
from grocery_agent.meals.catalog import MealCatalog
from grocery_agent.recipe_config import RecipeSettings
from grocery_agent.recipe_service import RecipeService
from grocery_agent.recipes import CodexProvider, RecipeRequest, draft_schema
from tests.test_meals import NOW, groceries

ROOT = Path(__file__).resolve().parents[1]


class SequenceProvider:
    def __init__(self, *responses: Any) -> None:
        self.responses = iter(responses)
        self.calls: list[tuple[RecipeRequest, str]] = []

    def generate(self, request: RecipeRequest, context: str) -> dict[str, Any]:
        self.calls.append((request, context))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def chicken_draft(grams: str = "700", title: str = "Chicken") -> dict[str, Any]:
    return {
        "title": title,
        "ingredients": [{"ingredient": "chicken", "quantity": grams}],
        "steps": ["Cook the chicken thoroughly and serve."],
        "minutes": 20,
    }


@pytest.fixture
def batch_service(candidate: dict[str, Any], tmp_path: Path) -> Any:
    settings = Settings(
        meal_config=ROOT / "config/meals.toml", report_dir=tmp_path / "reports", _env_file=None
    )
    service = RecipeService(settings, RecipeSettings(), lambda: NOW)
    catalogue = MealCatalog.load(settings.meal_config)
    rows = groceries(candidate)
    snapshot = {
        "run_id": "pinned-run",
        "source_id": "kupi",
        "finished_at": NOW.isoformat(),
        "warnings": [],
        "latest_run_status": "success",
        "source_scopes": {"kupi": [catalogue.policy.scope]},
        "offers": [
            {
                "offer": row.offer.model_dump(mode="json", exclude_computed_fields=True),
                "observed_at": row.observed_at.isoformat(),
                "observation_id": str(index),
                "source_id": "kupi",
                "run_id": "pinned-run",
            }
            for index, row in enumerate(rows)
        ],
    }
    calls: list[RecipeRequest] = []

    class Cache:
        def scopes(self, *args: Any) -> dict[str, tuple[str, ...]]:
            return {"kupi": (catalogue.policy.scope,)}

        def pin(self, request: RecipeRequest, base: MealCatalog) -> dict[str, Any]:
            calls.append(request)
            return deepcopy(snapshot)

    service.cache = Cache()
    return service, calls


@pytest.mark.parametrize("count", [1, 2, 6])
def test_batch_calls_once_per_draft_and_pins_once(batch_service: Any, count: int) -> None:
    service, pins = batch_service
    provider = SequenceProvider(*(chicken_draft(str(700 + i * 10)) for i in range(count)))
    service.provider = provider
    request = RecipeRequest(recipe_count=count)
    result, html = service.run(request)
    assert pins == [request] and len(provider.calls) == count
    assert result["batch"] == {
        "requested_count": count,
        "generated_count": count,
        "draft_count": count,
        "failures": [],
    }
    assert len(result["report"]["meals"]) == count
    assert result["report"]["run_id"] == "pinned-run"
    for meal in result["report"]["meals"]:
        price = meal["lines"][0]["price"]
        assert price["run_id"] == "pinned-run"
        assert price["offer"]["source_url"] in html
    schemas = [draft_schema(context) for _, context in provider.calls]
    assert all(schema == schemas[0] for schema in schemas)


def test_partial_invalid_and_timeout_continue_without_fallback(batch_service: Any) -> None:
    service, _ = batch_service
    service.provider = SequenceProvider(
        {}, {}, httpx.ReadTimeout("private details"), chicken_draft()
    )
    result, html = service.run(RecipeRequest(recipe_count=3))
    assert len(service.provider.calls) == 4
    assert "Repair the draft" in service.provider.calls[1][1]
    assert "Repair the draft" not in service.provider.calls[2][1]
    assert result["status"] == "ok"
    assert result["batch"] == {
        "requested_count": 3,
        "generated_count": 1,
        "draft_count": 1,
        "failures": [
            {"index": 1, "status": "failed", "code": "invalid-draft"},
            {"index": 2, "status": "failed", "code": "provider-timeout"},
        ],
    }
    assert "Recipe batch" in html and "private details" not in str(result)


def test_one_repair_per_item_and_independent_context(batch_service: Any) -> None:
    service, _ = batch_service
    service.provider = SequenceProvider({}, chicken_draft(), {}, chicken_draft("720"))
    result, _ = service.run(RecipeRequest(recipe_count=2))
    assert result["batch"]["generated_count"] == 2
    assert result["batch"]["draft_count"] == 2
    assert len(service.provider.calls) == 4
    assert "Repair" not in service.provider.calls[2][1]


@pytest.mark.parametrize("error", [RuntimeError("unavailable"), TimeoutError("timeout"), {}])
def test_all_provider_failures_raise_explicit_error(batch_service: Any, error: Any) -> None:
    service, _ = batch_service
    service.provider = SequenceProvider(*([error] * 4))
    with pytest.raises(RuntimeError, match="no valid provider drafts"):
        service.run(RecipeRequest(recipe_count=2))
    assert len(service.provider.calls) == (4 if isinstance(error, dict) else 2)


def test_all_successful_but_infeasible_is_not_provider_failure(batch_service: Any) -> None:
    service, _ = batch_service
    service.provider = SequenceProvider(chicken_draft("1"), chicken_draft("2"))
    result, html = service.run(RecipeRequest(recipe_count=2, model="fixture-model"))
    assert result["status"] == "no-feasible-recipe"
    assert result["batch"]["generated_count"] == 0
    assert result["batch"]["draft_count"] == 2
    assert len(result["batch"]["failures"]) == 2
    assert all(item["status"] == "no-feasible-recipe" for item in result["batch"]["failures"])
    assert "Recipe batch" in html
    assert result["provider"] == "codex"
    assert result["model"] == "fixture-model"
    assert result["prompt_version"] == "recipe-draft-v2"


@pytest.mark.parametrize(
    "error,code",
    [
        (subprocess.TimeoutExpired("codex", 5, stderr="private-secret"), "provider-timeout"),
        (
            subprocess.CalledProcessError(1, "codex", stderr="private-secret timed out"),
            "provider-failed",
        ),
        (OSError("private-secret timed out"), "provider-failed"),
    ],
)
def test_codex_batch_distinguishes_timeout_from_other_process_failures(
    batch_service: Any, monkeypatch: pytest.MonkeyPatch, error: Exception, code: str
) -> None:
    import json

    service, _ = batch_service
    calls: list[Any] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if len(calls) == 1:
            raise error
        return subprocess.CompletedProcess(command, 0, json.dumps(chicken_draft()))

    monkeypatch.setattr(subprocess, "run", run)
    service.provider = CodexProvider(timeout=5)
    result, html = service.run(RecipeRequest(recipe_count=2))
    assert len(calls) == 2
    assert result["batch"]["failures"] == [{"index": 1, "status": "failed", "code": code}]
    assert result["batch"]["generated_count"] == 1
    assert "private-secret" not in str(result) + html


def test_timeout_wording_alone_does_not_classify_provider_as_timeout(batch_service: Any) -> None:
    service, _ = batch_service
    service.provider = SequenceProvider(RuntimeError("failed or timed out"), chicken_draft())
    result, _ = service.run(RecipeRequest(recipe_count=2))
    assert result["batch"]["failures"][0]["code"] == "provider-failed"


def test_infeasible_item_does_not_drop_feasible_item(batch_service: Any) -> None:
    service, _ = batch_service
    service.provider = SequenceProvider(chicken_draft("1"), chicken_draft())
    result, _ = service.run(RecipeRequest(recipe_count=2))
    assert result["status"] == "ok" and len(result["report"]["meals"]) == 1
    assert result["batch"]["generated_count"] == 1
    assert result["batch"]["draft_count"] == 2
    assert result["batch"]["failures"][0]["index"] == 1


def test_dedupe_ignores_reworded_title_and_decimal_spelling(batch_service: Any) -> None:
    service, _ = batch_service
    service.provider = SequenceProvider(chicken_draft(), chicken_draft("700.0", "Another title"))
    result, html = service.run(RecipeRequest(recipe_count=2))
    assert result["batch"]["generated_count"] == 1
    assert result["batch"]["draft_count"] == 2
    assert len(result["report"]["meals"]) == 1
    assert "duplicate" in html


def test_batch_ranking_matches_planner(batch_service: Any) -> None:
    service, _ = batch_service
    service.provider = SequenceProvider(chicken_draft("700", "A"), chicken_draft("740", "B"))
    result, _ = service.run(RecipeRequest(recipe_count=2))
    assert [meal["title"] for meal in result["report"]["meals"]] == ["B", "A"]


def test_none_keeps_one_model_draft_and_all_templates(batch_service: Any) -> None:
    service, _ = batch_service
    service.provider = SequenceProvider(chicken_draft())
    result, _ = service.run(RecipeRequest())
    assert len(service.provider.calls) == 1 and "batch" not in result
    request = RecipeRequest(provider="template")
    baseline, _ = service.run(request)
    assert "batch" not in baseline and len(baseline["report"]["meals"]) > 1
    inputs = service.prepare(request)
    original = deepcopy(inputs)
    result, _ = service.execute(request.model_copy(update={"recipe_count": 1}), inputs)
    assert inputs == original
    assert len(result["report"]["meals"]) <= 1
    assert len(service.provider.calls) == 1


@pytest.mark.parametrize("value", [0, 7, True, 1.0, "2"])
def test_count_is_bounded_integer(value: Any) -> None:
    with pytest.raises(ValidationError):
        RecipeRequest(recipe_count=value)


def test_draft_unknown_ids_get_only_one_repair(batch_service: Any) -> None:
    service, _ = batch_service
    bad = chicken_draft()
    bad["ingredients"][0]["ingredient"] = "salt"
    service.provider = SequenceProvider(bad, bad, chicken_draft())
    result, _ = service.run(RecipeRequest(recipe_count=2, condiments=("salt",)))
    assert len(service.provider.calls) == 3
    assert result["batch"]["failures"][0]["code"] == "invalid-draft"


@pytest.mark.parametrize("limit", [1, 2, 60])
def test_diversity_hint_is_bounded_even_for_six_large_drafts(
    batch_service: Any, limit: int
) -> None:
    import json

    service, _ = batch_service
    service.backend = RecipeSettings(context_ingredients=limit)
    responses = []
    for index in range(6):
        draft = chicken_draft(str(700 + index * 10), str(index) + "T" * 499)
        draft["steps"] = ["LONG_PRIVATE_STEP_" + "x" * 480] * 30
        responses.append(draft)
    service.provider = SequenceProvider(*responses)
    result, _ = service.run(
        RecipeRequest(recipe_count=6, pantry={"chicken": "available"}, use_first=("chicken",))
    )
    assert result["batch"]["generated_count"] == 6
    for index, (_, context) in enumerate(service.provider.calls):
        if index == 0:
            assert "Previous titles" not in context
            continue
        hint = context.split("\nCreate a distinct recipe. Previous titles: ", 1)[1]
        titles = json.loads(hint)
        assert len(titles) == min(index, limit, 5)
        assert all(len(title) == 80 for title in titles)
        assert len(hint) <= 5 * 84
        assert "LONG_PRIVATE_STEP" not in context and '"steps"' not in hint
        assert (
            len(
                draft_schema(context)["$defs"]["RecipeIngredient"]["properties"]["ingredient"][
                    "enum"
                ]
            )
            <= limit
        )


def test_multiple_stores_and_source_urls_are_preserved(batch_service: Any) -> None:
    service, _ = batch_service
    draft = chicken_draft()
    draft["ingredients"].append({"ingredient": "lentils", "quantity": "20"})
    request = RecipeRequest(recipe_count=1, max_stores=2)
    inputs = service.prepare(request)
    for row in inputs["offers"]:
        if row["offer"]["product"]["sku"] == "lentils":
            row["offer"]["product"]["store_id"] = "tesco"
    snapshot = deepcopy(inputs)
    service.provider = SequenceProvider(draft)
    result, html = service.execute(request, inputs)
    assert inputs == snapshot
    meal = result["report"]["meals"][0]
    assert set(meal["stores"]) == {"billa", "tesco"}
    assert all(line["price"]["offer"]["source_url"] in html for line in meal["lines"])
    service.provider = SequenceProvider(draft)
    constrained = deepcopy(inputs)
    constrained["catalog"]["policy"]["max_stores"] = 1
    result, _ = service.execute(request.model_copy(update={"max_stores": 1}), constrained)
    assert result["status"] == "no-feasible-recipe"
    assert result["batch"]["generated_count"] == 0


def test_saved_batch_report_is_immutable_and_replays_offline(batch_service: Any) -> None:
    import json

    from grocery_agent.apps.recipes import save_reports

    service, pins = batch_service
    request = RecipeRequest(recipe_count=2, condiments=("salt",))
    inputs = service.prepare(request)
    service.provider = SequenceProvider(chicken_draft(), chicken_draft("720"))
    result, html = service.execute(request, inputs)
    save_reports(service.settings, request, result, html, inputs=inputs)
    directory = service.settings.report_dir / "requests" / str(request.request_id)
    saved = {path.name: path.read_bytes() for path in directory.iterdir()}
    with pytest.raises(FileExistsError):
        save_reports(service.settings, request, {"changed": True}, "changed", inputs=inputs)
    assert saved == {path.name: path.read_bytes() for path in directory.iterdir()}
    assert json.loads(saved["report.json"])["batch"]["generated_count"] == 2
    service.provider = SequenceProvider(chicken_draft(), chicken_draft("720"))
    replay, replay_html = service.execute(request, json.loads(saved["inputs.json"]))
    assert replay == result and replay_html == html and pins == [request]


@pytest.mark.parametrize("style", ["breakfast", "snack"])
def test_template_batch_selects_requested_style(batch_service: Any, style: str) -> None:
    service, _ = batch_service
    result, _ = service.run(
        RecipeRequest(
            provider="template", recipe_count=1, meal_style=style, pantry={"oats": "available"}
        )
    )
    assert result["status"] == "ok"
    assert result["batch"]["generated_count"] == 1
    assert result["report"]["meals"][0]["meal_style"] == style
