"""Offline safeguards for the real-model benchmark; no inference in these tests."""

import json
from argparse import Namespace
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine

from grocery_agent.apps import benchmark_recipes
from grocery_agent.apps.benchmark_recipes import (
    CASES,
    WITNESSES,
    MeasuredProvider,
    build_inputs,
    case_inputs,
    evaluate_draft,
    summarize,
)
from grocery_agent.meals.catalog import MealCatalog
from grocery_agent.persistence.repository import SQLAlchemyOfferRepository
from grocery_agent.persistence.snapshots import FileSnapshotStore
from grocery_agent.recipes import RecipeRequest
from tests.application_support import NOW, complete_batch
from tests.test_meals import ROOT, groceries


@pytest.mark.parametrize("case", list(CASES))
def test_cases_have_feasible_witnesses_and_keep_database_read_only(
    engine: Engine,
    repository: SQLAlchemyOfferRepository,
    snapshots: FileSnapshotStore,
    candidate: dict[str, Any],
    case: str,
) -> None:
    complete_batch(
        repository, snapshots, [row.offer for row in groceries(candidate)], source="kupi"
    )
    base = MealCatalog.load(ROOT / "config/meals.toml")
    assert engine.url.database
    database = Path(engine.url.database)
    before = database.read_bytes()
    pinned, counts = build_inputs(base, database, NOW)
    assert counts["total"] == counts["eligible_registry_quotes"] == 6
    request = RecipeRequest(provider="ollama", **CASES[case])
    inputs = case_inputs(base, request, pinned)
    assert not set(request.exclusions) & inputs["ingredient_context"].keys()
    draft = {
        "title": case,
        "minutes": 25,
        "steps": ["Cook."],
        "ingredients": [
            {"ingredient": key, "quantity": quantity, "unit": "g"}
            for key, quantity in WITNESSES[case].items()
        ],
    }
    report = evaluate_draft(request, inputs, draft, NOW)
    assert report["meals"]
    assert database.read_bytes() == before


def test_measured_provider_keeps_prompt_fair_and_captures_raw_metrics() -> None:
    calls = []
    draft = {
        "title": "Dinner",
        "ingredients": [{"ingredient": "rice", "quantity": "200", "unit": "g"}],
        "steps": ["Cook."],
        "minutes": 20,
    }

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "response": json.dumps(draft),
                "eval_count": 100,
                "eval_duration": 1_000_000_000,
                "done_reason": "stop",
            },
        )

    with httpx.Client(base_url="http://localhost", transport=httpx.MockTransport(handle)) as client:
        request = RecipeRequest(provider="ollama", model="first")
        first = MeasuredProvider(client, "first", 240, 17)
        second = MeasuredProvider(client, "second", 240, 17)
        context = json.dumps({"rice": {"label": "Rice"}})
        assert first.generate(request, context) == second.generate(
            request.model_copy(update={"model": "second"}), context
        )
    assert calls[0]["prompt"] == calls[1]["prompt"]
    assert calls[0]["options"] == calls[1]["options"]
    assert first.calls[0]["prompt_sha256"] == second.calls[0]["prompt_sha256"]
    assert first.calls[0]["response"]["eval_count"] == 100
    assert first.calls[0]["wall_seconds"] >= 0
    assert calls[0]["format"]["$defs"]["RecipeIngredient"]["properties"]["ingredient"]["enum"] == [
        "rice"
    ]


@pytest.mark.parametrize("status,body", [(200, {"response": "invalid"}), (503, {})])
def test_provider_failures_retain_timing_evidence(status: int, body: dict[str, Any]) -> None:
    with httpx.Client(
        base_url="http://localhost",
        transport=httpx.MockTransport(lambda _: httpx.Response(status, json=body)),
    ) as client:
        provider = MeasuredProvider(client, "fixture", 240, 17)
        with pytest.raises((ValueError, httpx.HTTPError)):
            provider.generate(RecipeRequest(), "{}")
        assert len(provider.calls) == 1
        assert provider.calls[0]["wall_seconds"] >= 0
        assert provider.calls[0]["error"]
        assert provider.calls[0]["error_type"]


def test_summary_distinguishes_repaired_success_from_failure() -> None:
    draft = {
        "title": "Dinner",
        "ingredients": [{"ingredient": "rice", "quantity": "200", "unit": "g"}],
        "steps": ["Cook."],
        "minutes": 20,
    }
    body = {
        "response": json.dumps(draft),
        "eval_count": 100,
        "eval_duration": 2_000_000_000,
    }
    evidence = {
        "models": [{"name": "fixture"}],
        "cases": {"dinner": {"inputs": {"ingredient_context": {"rice": {}}}}},
        "runs": [
            {
                "model": "fixture",
                "case": "dinner",
                "status": "ok",
                "wall_seconds": 130,
                "calls": [
                    {"error": "invalid", "wall_seconds": 100},
                    {"response": body, "wall_seconds": 30},
                ],
            },
            {
                "model": "fixture",
                "case": "dinner",
                "status": "provider-failure",
                "wall_seconds": 240,
                "calls": [{"error": "timeout"}],
            },
        ],
    }
    result = summarize(evidence)["fixture"]
    assert result["first_valid_drafts"] == 0
    assert result["final_valid_drafts"] == 1
    assert result["requests_with_repair"] == 1
    assert result["median_success_seconds"] == 130
    assert result["successful_within_120_seconds"] == 0
    assert result["successful_all_calls_within_120_seconds"] == 1
    assert result["weighted_output_tokens_per_second"] == 50


def test_timeout_retains_type_even_when_exception_message_is_empty() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("", request=request)

    with httpx.Client(base_url="http://localhost", transport=httpx.MockTransport(handle)) as client:
        provider = MeasuredProvider(client, "fixture", 240, 17)
        with pytest.raises(httpx.ReadTimeout):
            provider.generate(RecipeRequest(), "{}")
    assert provider.calls[0]["error_type"] == "ReadTimeout"
    assert "response" not in provider.calls[0]
    assert provider.calls[0]["wall_seconds"] >= 0


def test_replay_uses_pinned_inputs_without_reading_database(
    engine: Engine,
    repository: SQLAlchemyOfferRepository,
    snapshots: FileSnapshotStore,
    candidate: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    complete_batch(
        repository, snapshots, [row.offer for row in groceries(candidate)], source="kupi"
    )
    assert engine.url.database
    base = MealCatalog.load(ROOT / "config/meals.toml")
    pinned, counts = build_inputs(base, Path(engine.url.database), NOW)
    cases, drafts = {}, {}
    for name, parameters in CASES.items():
        request = RecipeRequest(provider="ollama", **parameters)
        inputs = case_inputs(base, request, pinned)
        draft = {
            "title": name,
            "minutes": 25,
            "steps": ["Cook."],
            "ingredients": [
                {"ingredient": key, "quantity": grams, "unit": "g"}
                for key, grams in WITNESSES[name].items()
            ],
        }
        drafts[str(request.request_id)] = draft
        cases[name] = {
            "request": request.model_dump(mode="json"),
            "inputs": inputs,
            "feasibility_witness": evaluate_draft(request, inputs, draft, NOW),
        }
    previous = {
        "evaluation_at": NOW.isoformat(),
        "snapshot": pinned,
        "offer_counts": counts,
        "cases": cases,
    }
    source = tmp_path / "previous.json"
    source.write_text(json.dumps(previous))

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            value = {"models": [{"name": "fixture"}]}
        elif request.url.path == "/api/ps":
            value = {"models": []}
        elif request.url.path == "/api/version":
            value = {"version": "fixture"}
        else:
            payload = json.loads(request.content)
            prompt = json.loads(payload["prompt"]) if "prompt" in payload else None
            value = {
                "response": json.dumps(drafts[prompt["request"]["request_id"]]) if prompt else "",
            }
        return httpx.Response(200, json=value)

    client = httpx.Client(base_url="http://localhost", transport=httpx.MockTransport(handle))
    monkeypatch.setattr(benchmark_recipes.httpx, "Client", lambda **_: client)

    def unexpected_read(*_: Any) -> None:
        raise AssertionError("replay must not load a catalog or database")

    monkeypatch.setattr(benchmark_recipes, "build_inputs", unexpected_read)
    monkeypatch.setattr(MealCatalog, "load", unexpected_read)
    evidence = benchmark_recipes.run(
        Namespace(
            output=tmp_path / "replay",
            inputs_from=source,
            database=tmp_path / "absent.db",
            catalog=tmp_path / "absent.toml",
            api_url="http://localhost",
            timeout=240,
            models=["fixture"],
            repeats=1,
        )
    )
    assert evidence["evaluation_at"] == previous["evaluation_at"]
    assert evidence["snapshot"] == pinned
    assert evidence["cases"] == cases
    assert [row["status"] for row in evidence["runs"]] == ["ok"] * len(CASES)


def test_calendar_time_is_separate_from_monotonic_elapsed(monkeypatch: pytest.MonkeyPatch) -> None:
    timestamps = iter([NOW, NOW + timedelta(hours=1)])
    monkeypatch.setattr(benchmark_recipes, "utc_now", lambda: next(timestamps))
    with httpx.Client(
        base_url="http://localhost",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"response": "{}"})),
    ) as client:
        provider = MeasuredProvider(client, "fixture", 240, 17)
        provider.generate(RecipeRequest(), "{}")
    record = provider.calls[0]
    assert record["calendar_wall_seconds"] == 3600
    assert record["wall_seconds"] < 1
    assert record["finished_at"] != record["started_at"]
