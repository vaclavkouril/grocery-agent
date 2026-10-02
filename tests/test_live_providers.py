"""Explicitly opt-in real provider acceptance; never enabled by the default suite."""

import json
import os

import pytest

from grocery_agent.recipes import CodexProvider, OllamaProvider, RecipeDraft, RecipeRequest

pytestmark = pytest.mark.live

CONTEXT = json.dumps(
    {
        "ingredients": {
            "rice": {"label": "Rice", "available_grams": "200", "protein_per_100g": "7"},
            "chicken": {
                "label": "Chicken breast",
                "available_grams": "300",
                "protein_per_100g": "23",
            },
        },
        "instructions": "Use only rice and chicken. Do not run tools or inspect files.",
    }
)


def check_draft(value: dict) -> None:
    draft = RecipeDraft.model_validate(value)
    assert {item.ingredient for item in draft.ingredients} <= {"rice", "chicken"}
    assert draft.title and draft.steps


def test_live_codex_structured_draft() -> None:
    if os.environ.get("GROCERY_LIVE_CODEX") != "1":
        pytest.skip("set GROCERY_LIVE_CODEX=1 to use the authenticated Codex account")
    model = os.environ.get("GROCERY_LIVE_CODEX_MODEL")
    request = RecipeRequest(servings=1, model=model)
    check_draft(CodexProvider(model=model, timeout=60).generate(request, CONTEXT))


def test_live_ollama_structured_draft() -> None:
    model = os.environ.get("GROCERY_LIVE_OLLAMA_MODEL")
    if not model:
        pytest.skip("set GROCERY_LIVE_OLLAMA_MODEL to an installed local model")
    request = RecipeRequest(provider="ollama", servings=1, model=model)
    check_draft(OllamaProvider(model, timeout=120).generate(request, CONTEXT))
