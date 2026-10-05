"""Provider-neutral recipe contracts and deterministic request orchestration."""

from __future__ import annotations

import json
import subprocess
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol
from uuid import UUID, uuid4

import httpx
from pydantic import Field, field_validator, model_validator

from grocery_agent.models.common import DomainModel, ExactDecimal, NonEmpty, PositiveAmount, StoreId

NutritionLimit = Annotated[ExactDecimal, Field(ge=0, max_digits=16, decimal_places=6)]


class RecipeRequest(DomainModel):
    request_id: UUID = Field(default_factory=uuid4)
    language: Literal["cs", "en"] = "en"
    servings: int = Field(default=2, ge=1, le=20)
    meal_style: str = "main"
    pantry: dict[str, str] = Field(default_factory=dict)
    exclusions: tuple[str, ...] = ()
    max_cost_per_serving_czk: Decimal | None = Field(default=None, gt=0)
    max_stores: int = Field(default=1, ge=1, le=3)
    retailer_ids: tuple[StoreId, ...] = Field(default=(), max_length=64)
    allow_loyalty: Annotated[bool | None, Field(strict=True)] = None
    min_protein_g: Annotated[NutritionLimit | None, Field(le=300)] = None
    max_kcal: Annotated[NutritionLimit | None, Field(gt=0, le=10000)] = None
    max_minutes: Annotated[int | None, Field(strict=True, ge=1, le=480)] = None
    use_first: tuple[NonEmpty, ...] = Field(default=(), max_length=200)
    seasonings_available: Annotated[bool | None, Field(strict=True)] = None
    provider: Literal["codex", "ollama", "template"] = "codex"
    model: str | None = None
    cache_policy: Literal["local", "refresh", "no-cache", "cache-only"] = "local"
    source_ids: tuple[str, ...] = ("kupi",)
    profile_fingerprint: str | None = Field(default=None, pattern=r"^(?:legacy|[0-9a-f]{24})$")
    profile_fingerprints: dict[str, str] = Field(default_factory=dict)

    @field_validator("retailer_ids", "use_first")
    @classmethod
    def unique_selections(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values) or any(not value for value in values):
            raise ValueError("selections must contain unique nonempty IDs")
        return values

    @field_validator("source_ids")
    @classmethod
    def unique_sources(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values or len(values) > 4 or len(set(values)) != len(values):
            raise ValueError("select one to four unique acquisition sources")
        from pydantic import TypeAdapter

        from grocery_agent.models.common import StoreId

        return tuple(TypeAdapter(StoreId).validate_python(value) for value in values)

    @model_validator(mode="after")
    def aligned_profiles(self) -> RecipeRequest:
        import re

        if set(self.profile_fingerprints) - set(self.source_ids):
            raise ValueError("profile fingerprints must belong to selected sources")
        if any(
            re.fullmatch(r"legacy|[0-9a-f]{24}", value) is None
            for value in self.profile_fingerprints.values()
        ):
            raise ValueError("invalid acquisition profile fingerprint")
        if self.profile_fingerprint is not None and (
            len(self.source_ids) != 1 or self.profile_fingerprints
        ):
            raise ValueError("use per-source profile_fingerprints for combined requests")
        return self


class RecipeIngredient(DomainModel):
    ingredient: NonEmpty
    quantity: PositiveAmount
    unit: Literal["g"] = "g"


class RecipeDraft(DomainModel):
    title: NonEmpty
    ingredients: tuple[RecipeIngredient, ...] = Field(min_length=1, max_length=30)
    steps: tuple[NonEmpty, ...] = Field(min_length=1, max_length=30)
    minutes: int = Field(ge=1, le=480)


def draft_schema(context: str = "") -> dict[str, Any]:
    schema = RecipeDraft.model_json_schema()
    # The context begins with the pinned ingredient map; repair instructions may
    # follow it. Constrain IDs at generation time as well as deterministic validation.
    try:
        ingredients, _ = json.JSONDecoder().raw_decode(context.lstrip())
    except (ValueError, TypeError):
        ingredients = None
    if isinstance(ingredients, dict):
        ingredients = ingredients.get("ingredients", ingredients)
        if isinstance(ingredients, dict) and ingredients:
            schema["$defs"]["RecipeIngredient"]["properties"]["ingredient"]["enum"] = sorted(
                ingredients
            )
    # Pydantic's Decimal input schema includes a regex lookahead rejected by
    # real structured-output providers. Use the wire-format decimal string;
    # positivity/precision bounds remain enforced by RecipeDraft validation.
    schema["$defs"]["RecipeIngredient"]["properties"]["quantity"] = {
        "type": "string",
        "pattern": r"^[0-9]+(?:\.[0-9]{1,6})?$",
    }

    # Structured outputs require all object properties to be listed as required.
    def strict(node: Any) -> None:
        if isinstance(node, dict):
            if "properties" in node:
                node["required"] = list(node["properties"])
            node.pop("default", None)
            for value in node.values():
                strict(value)
        elif isinstance(node, list):
            for value in node:
                strict(value)

    strict(schema)
    return schema


def recipe_prompt(request: RecipeRequest, context: str) -> str:
    return json.dumps(
        {
            "task": "Write a recipe using only known ingredient IDs in context. "
            "Quantities are decimal strings of total raw grams for all requested servings. "
            "Return only a recipe draft; prices and nutrition are computed later.",
            "language": request.language,
            "instructions": "Write the title and all steps in "
            + ("Czech" if request.language == "cs" else "English")
            + ". Keep canonical ingredient IDs unchanged.",
            "request": request.model_dump(mode="json"),
            "context": context,
        }
    )


class RecipeResult(DomainModel):
    request_id: UUID
    title: str
    ingredients: tuple[RecipeIngredient, ...]
    steps: tuple[str, ...]
    servings: int
    provider: str
    model: str | None = None
    cost_czk: Decimal | None = None
    nutrition: dict[str, Decimal] = Field(default_factory=dict)
    source_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    minutes: int = Field(default=30, ge=1, le=480)


class RecipeProvider(Protocol):
    def generate(self, request: RecipeRequest, context: str) -> dict[str, Any]: ...


class TemplateProvider:
    def generate(self, request: RecipeRequest, context: str) -> dict[str, Any]:
        return {
            "title": "Jednoduchá pánev" if request.language == "cs" else "Simple skillet",
            "ingredients": [{"ingredient": "zelenina", "quantity": 300}],
            "steps": ["Promíchejte a podávejte." if request.language == "cs" else "Mix and serve."],
            "minutes": 15,
        }


class CodexProvider:
    """Small, injectable Codex subprocess adapter; no filesystem access is granted."""

    def __init__(self, model: str | None = None, timeout: float = 120) -> None:
        self.model, self.timeout = model, timeout

    def generate(self, request: RecipeRequest, context: str) -> dict[str, Any]:
        prompt = recipe_prompt(request, context)
        with tempfile.TemporaryDirectory(prefix="grocery-codex-") as cwd:
            schema_path = Path(cwd) / "schema.json"
            schema_path.write_text(json.dumps(draft_schema(context)), encoding="utf-8")
            command = [
                "codex",
                "exec",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--output-schema",
                str(schema_path),
                "-",
            ]
            if self.model:
                command[2:2] = ["--model", self.model]
            try:
                completed = subprocess.run(
                    command,
                    input=prompt,
                    cwd=cwd,
                    text=True,
                    capture_output=True,
                    timeout=self.timeout,
                    check=True,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise RuntimeError("Codex provider failed or timed out") from exc
        try:
            value = json.loads(completed.stdout)
            if not isinstance(value, dict):
                raise RuntimeError("Codex provider returned a non-object draft")
            return value
        except json.JSONDecodeError as exc:
            raise RuntimeError("Codex provider returned invalid JSON") from exc


class OllamaProvider:
    def __init__(
        self, model: str, base_url: str = "http://127.0.0.1:11434", timeout: float = 120
    ) -> None:
        self.model, self.base_url, self.timeout = model, base_url.rstrip("/"), timeout

    def generate(self, request: RecipeRequest, context: str) -> dict[str, Any]:
        schema = draft_schema(context)
        response = httpx.post(
            f"{self.base_url}/api/generate",
            json={
                "model": self.model,
                "prompt": recipe_prompt(request, context),
                "format": schema,
                "stream": False,
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        body = response.json().get("response", "")
        try:
            value = json.loads(body)
            if not isinstance(value, dict):
                raise RuntimeError("Ollama provider returned a non-object draft")
            return value
        except json.JSONDecodeError as exc:
            raise RuntimeError("Ollama provider returned invalid JSON") from exc


def validate_result(request: RecipeRequest, draft: dict[str, Any]) -> RecipeResult:
    validated = RecipeDraft.model_validate(draft)
    result = RecipeResult.model_validate(
        {
            **validated.model_dump(),
            "request_id": request.request_id,
            "servings": request.servings,
            "provider": request.provider,
            "model": request.model,
            "source_ids": request.source_ids,
        }
    )
    if not result.ingredients or not result.steps:
        raise ValueError("recipe must contain ingredients and preparation steps")
    return result


def generate_recipe(
    request: RecipeRequest, context: str = "", provider: RecipeProvider | None = None
) -> RecipeResult:
    if provider is None:
        if request.provider == "template":
            provider = TemplateProvider()
        elif request.provider == "ollama":
            if not request.model:
                raise ValueError("Ollama requires a configured model")
            provider = OllamaProvider(request.model)
        else:
            provider = CodexProvider(request.model)
    try:
        return validate_result(request, provider.generate(request, context))
    except (ValueError, RuntimeError):
        raise
