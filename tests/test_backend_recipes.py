import json
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, select

from grocery_agent.backend.config import BackendSettings
from grocery_agent.backend.recipes import RecipeExecutor
from grocery_agent.backend.repository import ControlRepository
from grocery_agent.backend.worker import Worker
from grocery_agent.catalogue.repository import SQLAlchemyCatalogueRepository
from grocery_agent.config import Settings
from grocery_agent.persistence.control.schema import JobRow
from grocery_agent.persistence.database import create_database_engine
from grocery_agent.persistence.migrations import upgrade_database
from grocery_agent.persistence.repository import SQLAlchemyOfferRepository
from grocery_agent.persistence.snapshots import FileSnapshotStore
from grocery_agent.recipes import CodexProvider, OllamaProvider, RecipeRequest
from tests.application_support import NOW, complete_batch
from tests.test_meals import groceries


def test_provider_schema_constrains_pinned_ids_even_with_repair_suffix() -> None:
    from grocery_agent.recipes import draft_schema

    schema = draft_schema('{"rice": {}, "chicken": {}}\nRepair invalid quantities.')
    assert schema["$defs"]["RecipeIngredient"]["properties"]["ingredient"]["enum"] == [
        "chicken",
        "rice",
    ]


ROOT = Path(__file__).resolve().parents[1]


class DraftProvider:
    def __init__(self, failures: int = 0) -> None:
        self.calls = 0
        self.failures = failures

    def generate(self, request: RecipeRequest, context: str) -> dict[str, Any]:
        self.calls += 1
        return {
            "title": "Model chicken",
            "minutes": 20,
            "ingredients": [
                {
                    "ingredient": "unknown" if self.calls <= self.failures else "chicken",
                    "quantity": "800",
                    "unit": "g",
                }
            ],
            "steps": ["Cook the chicken thoroughly."],
        }


@pytest.fixture
def executor_inputs(
    engine: Engine,
    repository: SQLAlchemyOfferRepository,
    snapshots: FileSnapshotStore,
    candidate: dict[str, Any],
) -> Any:
    run = complete_batch(
        repository, snapshots, [item.offer for item in groceries(candidate)], source="kupi"
    )
    SQLAlchemyCatalogueRepository(engine).publish(run.run_id)
    settings = Settings(
        database_url=str(engine.url), meal_config=ROOT / "config/meals.toml", _env_file=None
    )
    executor = RecipeExecutor(settings, BackendSettings(), lambda: NOW)
    request = RecipeRequest(provider="codex", pantry={"chicken": "100g"}, cache_policy="cache-only")
    return executor, request, executor.prepare(request)


def test_pinned_generation_decimal_evaluation_and_html_escaping(executor_inputs: Any) -> None:
    executor, request, inputs = executor_inputs
    provider = DraftProvider()
    executor.provider = provider
    result, html = executor.execute(request, inputs)
    assert result["status"] == "ok"
    meal = result["report"]["meals"][0]
    assert meal["title"] == "Model chicken"
    assert meal["lines"][0]["owned_grams"] == "100"
    assert meal["lines"][0]["purchased_grams"] == "700"
    assert meal["usage_cost_per_serving_czk"] == "38.00"
    assert result["report"]["run_id"] == inputs["run_id"]
    assert "Model chicken" in html and provider.calls == 1


def test_one_repair_limit_and_no_feasible_is_not_infrastructure_failure(
    executor_inputs: Any,
) -> None:
    executor, request, inputs = executor_inputs
    executor.provider = DraftProvider(failures=1)
    assert executor.execute(request, inputs)[0]["status"] == "ok"
    assert executor.provider.calls == 2
    executor.provider = DraftProvider(failures=3)
    with pytest.raises(ValueError, match="one repair"):
        executor.execute(request, inputs)
    assert executor.provider.calls == 2
    executor.provider = DraftProvider()
    inputs["catalog"]["policy"]["max_cost_per_serving_czk"] = "1"
    assert executor.execute(request, inputs)[0]["status"] == "no-feasible-recipe"


def test_template_evaluates_existing_recipes(executor_inputs: Any) -> None:
    executor, request, inputs = executor_inputs
    request = RecipeRequest.model_validate({**request.model_dump(), "provider": "template"})
    result, html = executor.execute(request, inputs)
    assert result["status"] == "ok" and len(result["report"]["meals"]) == 4
    assert "Model chicken" not in html


def test_codex_stdin_schema_read_only_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    draft = DraftProvider().generate(RecipeRequest(), "")

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert command[-1] == "-" and command[command.index("--sandbox") + 1] == "read-only"
        assert "pantry" in kwargs["input"] and kwargs["timeout"] == 5
        schema = json.loads(Path(command[command.index("--output-schema") + 1]).read_text())
        assert schema["required"] == ["title", "ingredients", "steps", "minutes"]
        quantity = schema["$defs"]["RecipeIngredient"]["properties"]["quantity"]
        assert quantity["type"] == "string"
        assert "(?" not in quantity["pattern"].replace("(?:", "")
        assert schema["$defs"]["RecipeIngredient"]["required"] == ["ingredient", "quantity", "unit"]
        return subprocess.CompletedProcess(command, 0, json.dumps(draft))

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert CodexProvider(timeout=5).generate(RecipeRequest(), "") == draft

    def timeout(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired("codex", 5, stderr="private-path-and-secret")

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match="timed out") as error:
        CodexProvider(timeout=5).generate(RecipeRequest(), "")
    assert "private" not in str(error.value)


def test_ollama_uses_draft_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    draft = DraftProvider().generate(RecipeRequest(), "")

    def post(url: str, **kwargs: Any) -> httpx.Response:
        assert url == "http://127.0.0.1:11434/api/generate"
        assert kwargs["json"]["model"] == "fixture-model" and not kwargs["json"]["stream"]
        assert "request_id" not in kwargs["json"]["format"]["properties"]
        return httpx.Response(
            200, json={"response": json.dumps(draft)}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx, "post", post)
    assert (
        OllamaProvider("fixture-model").generate(RecipeRequest(model="fixture-model"), "") == draft
    )


@pytest.mark.asyncio
async def test_worker_pins_and_persists_real_recipe_and_reconciles_refresh(
    executor_inputs: Any, tmp_path: Path
) -> None:
    executor, request, inputs = executor_inputs
    control = create_database_engine(f"sqlite:///{tmp_path / 'worker-control.db'}")
    upgrade_database(control, "control")
    repo = ControlRepository(control)
    user = repo.authenticate(repo.bootstrap("admin", NOW), NOW).user_id
    try:
        executor.provider = DraftProvider()
        submitted = repo.submit(user, request.model_dump(mode="json"), "recipe", NOW)
        worker = Worker(repo, executor, executor.settings, BackendSettings(), lambda: NOW)
        assert await worker.once()
        assert repo.result(user, submitted.job_id)[0]["status"] == "ok"
        with repo.sessions() as session:
            stored = session.get(JobRow, submitted.job_id)
            assert stored.inputs["run_id"] == inputs["run_id"]
            assert stored.inputs["catalog"]["pantry"]["items"]["chicken"]["grams"] == "100"
            assert stored.state == "succeeded" and stored.report_html
        refresh = repo.submit(user, {"source_id": "kupi"}, "refresh", NOW, kind="refresh")
        interrupted = repo.claim(NOW, 10, 3)
        assert interrupted
        repo.pin(interrupted, {"run_id": inputs["run_id"]}, NOW)
        later = NOW + timedelta(seconds=11)
        recovered = Worker(repo, executor, executor.settings, BackendSettings(), lambda: later)
        assert await recovered.once()
        assert repo.result(user, refresh.job_id)[0]["recovered"]
        with repo.sessions() as session:
            assert session.scalar(select(JobRow.attempts).where(JobRow.id == refresh.job_id)) == 2
    finally:
        control.dispose()
