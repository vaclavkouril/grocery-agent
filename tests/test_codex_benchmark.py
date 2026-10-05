"""Offline tests for cloud benchmark accounting and process isolation."""

import json
import subprocess
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from grocery_agent.apps import benchmark_codex
from grocery_agent.apps.benchmark_codex import MeasuredCodexProvider, usage_summary
from grocery_agent.apps.benchmark_recipes import MeasuredProvider
from grocery_agent.recipes import RecipeRequest, recipe_prompt


def test_codex_captures_usage_without_account_ids_or_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = {
        "title": "Oats",
        "ingredients": [{"ingredient": "oats", "quantity": "100", "unit": "g"}],
        "steps": ["Simmer in water."],
        "minutes": 15,
    }
    commands = []
    original = RecipeRequest(model="fixture")

    def execute(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        assert kwargs["input"] == recipe_prompt(
            original.model_copy(update={"provider": "ollama", "model": None}), "{}"
        )
        assert kwargs["cwd"].startswith("/tmp/grocery-benchmark-codex-")
        path = Path(command[command.index("--output-last-message") + 1])
        path.write_text(json.dumps(draft))
        events = [
            {"type": "thread.started", "thread_id": "private-id"},
            {"type": "item.completed", "item": {"type": "reasoning", "text": "private reasoning"}},
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 1000,
                    "cached_input_tokens": 800,
                    "output_tokens": 100,
                    "reasoning_output_tokens": 20,
                },
            },
        ]
        return subprocess.CompletedProcess(command, 0, "\n".join(map(json.dumps, events)), "")

    monkeypatch.setattr(benchmark_codex.subprocess, "run", execute)
    provider = MeasuredCodexProvider("fixture")
    assert provider.generate(original, "{}") == draft
    assert "--ignore-user-config" in commands[0]
    assert commands[0][commands[0].index("--sandbox") + 1] == "read-only"
    assert "private" not in json.dumps(provider.calls)
    assert provider.calls[0]["response"]["eval_count"] == 100
    assert provider.calls[0]["wall_seconds"] >= 0


@pytest.mark.parametrize("failure", ["timeout", "exit", "missing", "malformed", "list"])
def test_codex_failure_retains_evidence(monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    def execute(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 240)
        path = Path(command[command.index("--output-last-message") + 1])
        if failure in {"malformed", "list"}:
            path.write_text("invalid" if failure == "malformed" else "[]")
        return subprocess.CompletedProcess(command, 1 if failure == "exit" else 0, "", "")

    monkeypatch.setattr(benchmark_codex.subprocess, "run", execute)
    provider = MeasuredCodexProvider("fixture")
    with pytest.raises(RuntimeError, match="benchmark provider failed"):
        provider.generate(RecipeRequest(), "{}")
    assert provider.calls[0]["error_type"]
    assert provider.calls[0]["calendar_wall_seconds"] >= 0


def test_usage_includes_repairs_and_does_not_double_count_reasoning() -> None:
    usage = {
        "input_tokens": 1000,
        "cached_input_tokens": 800,
        "output_tokens": 100,
        "reasoning_output_tokens": 20,
    }
    runs = [
        {"calls": [{"usage_events": [usage]}, {"usage_events": [usage]}]},
        {"calls": [{"usage_events": [usage]}]},
    ]
    result = usage_summary(runs)
    assert result["totals"]["output_tokens"] == 300
    assert result["mean_per_request"]["input_tokens"] == 1500
    assert result["weekly_scenarios"]["7"]["input_tokens"] == 10500


def test_missing_usage_is_unknown_not_free() -> None:
    result = usage_summary([{"calls": [{}]}])
    assert result["totals"]["input_tokens"] is None
    assert result["weekly_scenarios"]["21"]["output_tokens"] is None


def test_thinking_flag_is_explicit_when_selected() -> None:
    payloads = []

    def handle(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"response": "{}"})

    with httpx.Client(base_url="http://localhost", transport=httpx.MockTransport(handle)) as client:
        MeasuredProvider(client, "fixture", 240, 17, think=False).generate(RecipeRequest(), "{}")
    assert payloads[0]["think"] is False


def test_partial_usage_is_unknown_not_an_optimistic_average() -> None:
    usage = {"input_tokens": 1000, "cached_input_tokens": 0, "output_tokens": 100}
    result = usage_summary([{"calls": [{"usage_events": [usage]}, {}]}])
    assert result["totals"]["input_tokens"] is None


def test_quota_reader_handles_multiple_events_and_strips_account_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdin = BytesIO()
    proc = SimpleNamespace(
        stdin=stdin,
        stdout=SimpleNamespace(fileno=lambda: 123),
        terminate=lambda: None,
        wait=lambda **kwargs: None,
    )
    chunks = iter(
        [
            b'{"id":1,"result":{}}\n{"method":"notification"}\n',
            json.dumps(
                {
                    "id": 2,
                    "result": {
                        "rateLimits": {
                            "primary": {"usedPercent": 8, "windowDurationMins": 10080},
                            "planType": "prolite",
                            "accountId": "private-id",
                            "credits": {"balance": 500},
                        }
                    },
                }
            ).encode()
            + b"\n",
        ]
    )

    class Selector:
        def __enter__(self) -> "Selector":
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def register(self, *args: Any) -> None:
            pass

        def select(self, *args: Any) -> list[bool]:
            return [True]

    monkeypatch.setattr(benchmark_codex.subprocess, "Popen", lambda *args, **kwargs: proc)
    monkeypatch.setattr(benchmark_codex.selectors, "DefaultSelector", Selector)
    monkeypatch.setattr(benchmark_codex.os, "read", lambda *args: next(chunks))
    result = benchmark_codex.read_limits()
    assert result["buckets"]["codex"]["primary"]["usedPercent"] == 8
    assert b"account/rateLimits/read" in stdin.getvalue()
    assert "private" not in json.dumps(result)
    assert "balance" not in json.dumps(result)
