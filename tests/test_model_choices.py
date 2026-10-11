"""Model discovery and enforcement without inference or acquisition."""

import json
import subprocess
from typing import Any

import pytest
from pydantic import ValidationError

from grocery_agent.backend.config import BackendSettings
from grocery_agent.model_choices import BENCHMARKED_LOCAL_MODELS, TESTED_CODEX_MODELS
from grocery_agent.recipes import CodexProvider, RecipeRequest
from tests.application_support import NOW
from tests.test_backend_cache_policies import backend_fixture as backend_fixture


def test_defaults_match_packaged_configuration() -> None:
    backend = BackendSettings.load()
    assert backend.codex_models == TESTED_CODEX_MODELS
    assert backend.ollama_models == BENCHMARKED_LOCAL_MODELS
    assert "ollama" in backend.providers
    assert "qwen2.5-coder:0.5b" in backend.ollama_models


@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-5.1-codex-mini", "unknown"])
def test_untested_codex_configuration_rejected(model: str) -> None:
    with pytest.raises(ValidationError, match="recipe-benchmarked"):
        BackendSettings(codex_models=(model,))


@pytest.mark.parametrize("field", ["codex_models", "ollama_models"])
def test_duplicate_model_configuration_rejected(field: str) -> None:
    with pytest.raises(ValidationError, match="unique"):
        BackendSettings(**{field: ("gpt-6-luna", "gpt-6-luna")})


def test_discovery_includes_weaker_local_models(backend_fixture: Any) -> None:
    _, _, headers, client = backend_fixture
    caps = client(BackendSettings()).get("/v1/capabilities", headers=headers).json()
    assert caps["models"]["codex"] == list(TESTED_CODEX_MODELS)
    assert caps["models"]["ollama"] == list(BENCHMARKED_LOCAL_MODELS)
    assert caps["defaults"]["model"] == TESTED_CODEX_MODELS[0]


@pytest.mark.parametrize("model", [None, *TESTED_CODEX_MODELS])
def test_codex_submission_pins_explicit_tested_model(
    backend_fixture: Any, model: str | None
) -> None:
    repo, _, headers, client = backend_fixture
    app = client(BackendSettings())
    response = app.post("/v1/recipes", headers=headers, json={"provider": "codex", "model": model})
    assert response.status_code == 202, response.text
    job = repo.claim(NOW, 180, 3)
    assert job and job.request["model"] == (model or TESTED_CODEX_MODELS[0])


@pytest.mark.parametrize("model", ["expensive-model", "gpt-6-astra", ""])
def test_codex_submission_cannot_bypass_allowlist(backend_fixture: Any, model: str) -> None:
    _, _, headers, client = backend_fixture
    app = client(BackendSettings())
    response = app.post("/v1/recipes", headers=headers, json={"provider": "codex", "model": model})
    assert response.status_code == 422


def test_empty_codex_allowlist_prevents_cli_default(backend_fixture: Any) -> None:
    _, _, headers, client = backend_fixture
    app = client(BackendSettings(codex_models=()))
    assert app.post("/v1/recipes", headers=headers, json={"provider": "codex"}).status_code == 422


def test_presets_keep_weaker_local_choice_and_reject_untested_codex(backend_fixture: Any) -> None:
    _, _, headers, client = backend_fixture
    app = client(BackendSettings())
    response = app.post(
        "/v1/me/presets",
        headers=headers,
        json={
            "name": "Small local experiment",
            "parameters": {"provider": "ollama", "model": "qwen2.5-coder:0.5b"},
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["parameters"]["model"] == "qwen2.5-coder:0.5b"
    denied = app.post(
        "/v1/me/presets",
        headers=headers,
        json={"name": "Denied", "parameters": {"provider": "codex", "model": "gpt-6-astra"}},
    )
    assert denied.status_code == 422


@pytest.mark.parametrize("model", BENCHMARKED_LOCAL_MODELS)
def test_every_benchmarked_local_model_is_selectable(backend_fixture: Any, model: str) -> None:
    _, _, headers, client = backend_fixture
    app = client(BackendSettings())
    response = app.post("/v1/recipes", headers=headers, json={"provider": "ollama", "model": model})
    assert response.status_code == 202, response.text


@pytest.mark.parametrize("model", TESTED_CODEX_MODELS)
def test_tested_codex_uses_low_reasoning(model: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert command[command.index("--model") + 1] == model
        assert 'model_reasoning_effort="low"' in command
        return subprocess.CompletedProcess(command, 0, json.dumps({"title": "Fixture"}))

    monkeypatch.setattr(subprocess, "run", run)
    assert CodexProvider(model).generate(RecipeRequest(model=model), "") == {"title": "Fixture"}
