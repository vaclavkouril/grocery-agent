"""Offline parity checks for request, CLI, text commands and deterministic planning."""

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from grocery_agent.apps import recipes as cli
from grocery_agent.config import Settings
from grocery_agent.contracts import Capabilities
from grocery_agent.meals.catalog import MealCatalog
from grocery_agent.meals.planner import plan_meals
from grocery_agent.recipe_config import RecipeSettings
from grocery_agent.recipe_service import RecipeService, effective_catalog
from grocery_agent.recipes import RecipeRequest
from tests.test_meals import NOW, MemoryReader, groceries


@pytest.fixture
def catalog() -> MealCatalog:
    return MealCatalog.load(Path(__file__).resolve().parents[1] / "config/meals.toml")


@pytest.fixture
def capabilities(catalog: MealCatalog) -> Capabilities:
    return Capabilities(
        providers=("template", "codex"),
        default_provider="template",
        models={},
        cache_policies=("cache-only",),
        default_cache_policy="cache-only",
        sources=("kupi",),
        scope=catalog.policy.scope,
        ingredients=tuple(catalog.ingredients),
        meal_styles=("main", "snack", "breakfast"),
        refresh_allowed=False,
        email_enabled=False,
        simplex_enabled=False,
    )


@pytest.mark.parametrize("field", ["min_protein_g", "max_kcal"])
@pytest.mark.parametrize(
    "value", [0.5, True, "-1", "NaN", "Infinity", "0.0000001", "10000000000000000"]
)
def test_nutrition_rejects_inexact_unbounded_values(field: str, value: Any) -> None:
    with pytest.raises(ValidationError):
        RecipeRequest.model_validate({field: value})


@pytest.mark.parametrize("value", [0, 481, 1.0, True, "30"])
def test_minutes_bounds_and_integer_type(value: Any) -> None:
    with pytest.raises(ValidationError):
        RecipeRequest(max_minutes=value)


@pytest.mark.parametrize(
    "payload",
    [
        {"retailer_ids": ["billa", "billa"]},
        {"retailer_ids": ["bad retailer"]},
        {"retailer_ids": ["a" * 65]},
        {"use_first": ["rice", "rice"]},
        {"use_first": [""]},
        {"allow_loyalty": "true"},
        {"seasonings_available": 1},
    ],
)
def test_selection_and_boolean_validation(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        RecipeRequest.model_validate(payload)


def test_bounded_exact_decimal_roundtrip() -> None:
    request = RecipeRequest(min_protein_g="12.340001", max_kcal="10000", max_minutes=480)
    assert request.min_protein_g == Decimal("12.340001")
    assert request.max_kcal == 10000
    assert RecipeRequest.model_validate_json(request.model_dump_json()) == request
    schema = RecipeRequest.model_json_schema()["properties"]
    assert schema["max_minutes"]["anyOf"][0]["maximum"] == 480
    assert schema["retailer_ids"]["maxItems"] == 64


@pytest.mark.parametrize(
    "payload",
    [
        {"min_protein_g": "300.000001"},
        {"max_kcal": "10000.000001"},
        {"max_kcal": "0"},
    ],
)
def test_nutrition_policy_bounds(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        RecipeRequest.model_validate(payload)


def test_retailer_overrides_only_narrow_configured_allowlist(catalog: MealCatalog) -> None:
    with pytest.raises(ValueError, match="subset"):
        effective_catalog(catalog, RecipeRequest(retailer_ids=("unconfigured",)))
    unrestricted = MealCatalog.model_validate(
        {
            **catalog.model_dump(),
            "policy": {**catalog.policy.model_dump(), "retailers": ()},
        }
    )
    assert effective_catalog(
        unrestricted,
        RecipeRequest(retailer_ids=("new_store",)),
    ).policy.retailers == ("new_store",)


def test_effective_defaults_style_and_explicit_overrides(catalog: MealCatalog) -> None:
    baseline = catalog.model_dump(mode="json")
    inherited = effective_catalog(catalog, RecipeRequest())
    assert inherited.policy.retailers == catalog.policy.retailers
    assert inherited.policy.allow_loyalty == catalog.policy.allow_loyalty
    resolved = effective_catalog(
        catalog,
        RecipeRequest(
            meal_style="snack",
            retailer_ids=("billa",),
            allow_loyalty=True,
            min_protein_g="0",
            max_kcal="1",
            max_minutes=15,
            pantry={"rice": "0.500125kg"},
            use_first=("rice",),
            seasonings_available=True,
        ),
    )
    assert resolved.policy.retailers == ("billa",)
    assert resolved.policy.allow_loyalty is True
    assert resolved.policy.min_protein_g == 0 and resolved.policy.max_kcal == 1
    assert resolved.policy.max_minutes == 15
    assert resolved.pantry.items["rice"].grams == Decimal("500.125")
    assert resolved.pantry.items["rice"].use_first is True
    assert resolved.pantry.seasonings_available is True
    assert catalog.model_dump(mode="json") == baseline


def test_use_first_requires_known_owned_stock(catalog: MealCatalog) -> None:
    for key in ("rice", "unknown"):
        with pytest.raises(ValueError):
            effective_catalog(catalog, RecipeRequest(use_first=(key,)))
    owned = effective_catalog(catalog, RecipeRequest(pantry={"rice": "available"}))
    resolved = effective_catalog(owned, RecipeRequest(use_first=("rice",)))
    assert resolved.pantry.items["rice"].use_first
    assert resolved.pantry.available_grams("rice", Decimal("123.45")) == Decimal("123.45")
    with pytest.raises(ValueError, match="excluded"):
        effective_catalog(
            catalog,
            RecipeRequest(
                pantry={"rice": "available"},
                use_first=("rice",),
                exclusions=("rice",),
            ),
        )


def test_explicit_false_overrides_config(catalog: MealCatalog) -> None:
    enabled = effective_catalog(
        catalog, RecipeRequest(allow_loyalty=True, seasonings_available=True)
    )
    disabled = effective_catalog(
        enabled, RecipeRequest(allow_loyalty=False, seasonings_available=False)
    )
    assert disabled.policy.allow_loyalty is False
    assert disabled.pantry.seasonings_available is False


def test_text_parity(capabilities: Capabilities) -> None:
    from grocery_agent.channel_commands import parse_channel_command

    request = parse_channel_command(
        "recipe retailer=billa,tesco allow_loyalty=false min_protein=12.340001 "
        "max_kcal=10000 max_minutes=480 have=rice=500g use_first=rice have_seasonings=true",
        capabilities,
    )
    assert isinstance(request, RecipeRequest)
    assert request.retailer_ids == ("billa", "tesco")
    assert request.allow_loyalty is False
    assert request.min_protein_g == Decimal("12.340001")
    assert request.max_kcal == 10000 and request.max_minutes == 480
    assert request.use_first == ("rice",) and request.seasonings_available is True


@pytest.mark.parametrize(
    "text",
    [
        "retailer=billa retailer_ids=tesco",
        "min_protein=1 min_protein_g=2",
        "have_seasonings=true seasonings_available=false",
        "max_minutes=481",
        "max_minutes=2.0",
        "max_minutes=+2",
        "max_minutes=２",
        "min_protein=-1",
        "max_kcal=NaN",
        "allow_loyalty=yes",
        "use_first=unknown",
        "retailer=billa,",
    ],
)
def test_text_rejects_invalid_parameters(text: str, capabilities: Capabilities) -> None:
    from grocery_agent.channel_commands import parse_channel_command

    with pytest.raises(ValueError):
        parse_channel_command("recipe " + text, capabilities)


def test_cli_parity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[RecipeRequest] = []

    class Service:
        def __init__(self, *args: Any) -> None:
            pass

        def prepare(self, request: RecipeRequest) -> dict[str, Any]:
            calls.append(request)
            return {}

        def execute(self, *args: Any) -> tuple[dict[str, Any], str]:
            return {}, ""

    monkeypatch.setattr(cli, "RecipeService", Service)
    monkeypatch.setattr(cli, "save_reports", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli.RecipeSettings, "load", lambda path: RecipeSettings())
    assert (
        cli.main(
            [
                "--config",
                str(tmp_path / "unused.toml"),
                "--retailer",
                "billa",
                "--retailer",
                "tesco",
                "--no-loyalty",
                "--min-protein",
                "12.340001",
                "--max-kcal",
                "10000",
                "--max-minutes",
                "480",
                "--have",
                "rice=500g",
                "--use-first",
                "rice",
                "--have-seasonings",
            ]
        )
        == 0
    )
    request = calls[0]
    assert request.retailer_ids == ("billa", "tesco") and request.allow_loyalty is False
    assert request.min_protein_g == Decimal("12.340001") and request.max_kcal == 10000
    assert request.max_minutes == 480 and request.use_first == ("rice",)
    assert request.seasonings_available is True
    with pytest.raises(SystemExit):
        cli.main(["--allow-loyalty", "--no-loyalty"])


def test_cli_uses_shared_default_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from grocery_agent import configuration

    paths: list[Path] = []
    path = tmp_path / "recipes.toml"
    monkeypatch.setattr(configuration, "default_config", lambda name: path)

    def read(selected: Path) -> dict[str, Any]:
        paths.append(selected)
        return {"default_provider": "template"}

    def service(settings: Settings, options: RecipeSettings) -> Any:
        assert options.default_provider == "template"
        raise ValueError("stop after configuration selection")

    monkeypatch.setattr(Path, "is_file", lambda self: self == path)
    monkeypatch.setattr(configuration, "read_toml", read)
    monkeypatch.setattr(cli, "RecipeService", service)
    assert cli.main([]) == 1
    assert paths == [path]


@pytest.mark.parametrize("environment", [False, True])
def test_cli_shared_config_layers_options_and_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    environment: bool,
) -> None:
    from grocery_agent import configuration

    shared, app = tmp_path / "shared.toml", tmp_path / "recipes.toml"
    payloads = {
        shared: {
            "shared": {"report_dir": "shared-reports", "meal_config": "shared-meals.toml"},
            "recipes": {"context_ingredients": 17, "default_provider": "ollama", "model": "shared"},
        },
        app: {"default_provider": "template", "model": "app"},
    }
    monkeypatch.setattr(configuration, "read_toml", lambda path: payloads[path])
    monkeypatch.delenv("GROCERY_SHARED_CONFIG", raising=False)
    monkeypatch.delenv("GROCERY_REPORT_DIR", raising=False)
    monkeypatch.delenv("GROCERY_MEAL_CONFIG", raising=False)
    for key in ("DEFAULT_PROVIDER", "MODEL", "CONTEXT_INGREDIENTS"):
        monkeypatch.delenv("GROCERY_RECIPES_" + key, raising=False)
    if environment:
        monkeypatch.setenv("GROCERY_REPORT_DIR", str(tmp_path / "env-reports"))
        monkeypatch.setenv("GROCERY_RECIPES_DEFAULT_PROVIDER", "codex")
        monkeypatch.setenv("GROCERY_RECIPES_CONTEXT_INGREDIENTS", "23")
        monkeypatch.setenv("GROCERY_RECIPES_MODEL", "env")
    captured: list[tuple[Settings, RecipeSettings, RecipeRequest]] = []

    class Service:
        def __init__(self, settings: Settings, options: RecipeSettings) -> None:
            self.settings, self.options = settings, options

        def prepare(self, request: RecipeRequest) -> dict[str, Any]:
            captured.append((self.settings, self.options, request))
            return {}

        def execute(self, *args: Any) -> tuple[dict[str, Any], str]:
            return {}, ""

    monkeypatch.setattr(cli, "RecipeService", Service)
    monkeypatch.setattr(cli, "save_reports", lambda *args, **kwargs: None)
    assert (
        cli.main(
            [
                "--shared-config",
                str(shared),
                "--config",
                str(app),
                "--model",
                "cli",
            ]
        )
        == 0
    )
    settings, options, request = captured[0]
    assert settings.report_dir == tmp_path / ("env-reports" if environment else "shared-reports")
    assert settings.meal_config == tmp_path / "shared-meals.toml"
    assert options.context_ingredients == (23 if environment else 17)
    assert options.model == ("env" if environment else "app")
    assert request.provider == ("codex" if environment else "template")
    assert request.model == "cli"
    assert configuration.shared_config_path() is None


@pytest.mark.parametrize("export", [False, True])
def test_remote_shared_config_exports_without_local_recipe_execution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capabilities: Capabilities,
    export: bool,
) -> None:
    from types import SimpleNamespace

    from grocery_agent import configuration

    shared = tmp_path / "shared.toml"
    monkeypatch.setenv("GROCERY_API_TOKEN", "fixture-token")
    monkeypatch.delenv("GROCERY_SHARED_CONFIG", raising=False)
    monkeypatch.delenv("GROCERY_REPORT_DIR", raising=False)
    monkeypatch.setattr(
        configuration,
        "read_toml",
        lambda path: {
            "shared": {"report_dir": "exports"},
            "recipes": {"default_provider": "ollama"},
        },
    )

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("remote execution must not load recipe options or acquire locally")

    monkeypatch.setattr(cli.RecipeSettings, "load", forbidden)
    monkeypatch.setattr(cli, "RecipeService", forbidden)
    saved: list[Settings] = []

    class Client:
        def __init__(self, *args: Any) -> None:
            pass

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def capabilities(self) -> Capabilities:
            return capabilities

        def submit(self, request: RecipeRequest) -> Any:
            assert request.provider == capabilities.default_provider
            return SimpleNamespace(job_id="fixture", model_dump_json=lambda: "{}")

        def wait(self, *args: Any, **kwargs: Any) -> Any:
            return SimpleNamespace(job_id="fixture", status="succeeded")

        def result(self, *args: Any) -> dict[str, Any]:
            return {}

        def report(self, *args: Any) -> str:
            return "<p>Remote</p>"

    monkeypatch.setattr(cli, "GroceryClient", Client)
    monkeypatch.setattr(
        cli, "save_reports", lambda settings, *args, **kwargs: saved.append(settings)
    )
    arguments = ["--api-url", "http://fixture", "--shared-config", str(shared)]
    arguments += ["--output-json", str(tmp_path / "export.json")] if export else ["--no-wait"]
    assert cli.main(arguments) == 0
    assert len(saved) == int(export)
    if export:
        assert saved[0].report_dir == tmp_path / "exports"
    assert configuration.shared_config_path() is None


def test_planner_time_limit_and_pantry_arithmetic(
    catalog: MealCatalog, candidate: dict[str, Any]
) -> None:
    rows = groceries(candidate)
    resolved = effective_catalog(catalog, RecipeRequest(max_minutes=30))
    report = plan_meals(MemoryReader(rows), resolved, NOW)
    assert report.meals and all(meal.minutes <= 30 for meal in report.meals)
    owned = effective_catalog(
        catalog,
        RecipeRequest(
            pantry={"chicken": "100g"},
            use_first=("chicken",),
            seasonings_available=True,
        ),
    )
    meal = plan_meals(MemoryReader(rows), owned, NOW).meals[0]
    chicken = next(line for line in meal.lines if line.price.ingredient_id == "chicken")
    assert chicken.owned_grams == 100
    assert chicken.purchased_grams == chicken.required_grams - chicken.owned_grams
    assert meal.use_first_grams == 100
    with pytest.raises(ValueError, match="no complete recipe"):
        plan_meals(
            MemoryReader(rows), effective_catalog(catalog, RecipeRequest(max_minutes=1)), NOW
        )


@pytest.mark.parametrize("limit", [{"max_minutes": 1}, {"min_protein_g": "300"}, {"max_kcal": "1"}])
def test_generated_infeasible_draft_is_not_repaired(
    catalog: MealCatalog,
    candidate: dict[str, Any],
    limit: dict[str, Any],
) -> None:
    class Provider:
        calls = 0

        def generate(self, *args: Any) -> dict[str, Any]:
            self.calls += 1
            return {
                "title": "Rice",
                "ingredients": [{"ingredient": "rice", "quantity": "200"}],
                "steps": ["Cook"],
                "minutes": 20,
            }

    request = RecipeRequest(**limit)
    resolved = effective_catalog(catalog, request)
    provider = Provider()
    service = RecipeService(Settings(), RecipeSettings(), clock=lambda: NOW, provider=provider)
    rows = groceries(candidate)
    inputs = {
        "run_id": "run",
        "source_id": "kupi",
        "finished_at": NOW.isoformat(),
        "warnings": [],
        "latest_run_status": "complete",
        "prompt_version": "test",
        "catalog": resolved.model_dump(mode="json"),
        "ingredient_context": {"rice": resolved.ingredients["rice"].model_dump(mode="json")},
        "offers": [
            {
                "offer": row.offer.model_dump(mode="json", exclude_computed_fields=True),
                "observed_at": NOW.isoformat(),
                "observation_id": None,
            }
            for row in rows
        ],
    }
    result, _ = service.execute(request, inputs)
    assert result["status"] == "no-feasible-recipe"
    assert provider.calls == 1


def test_prepare_retains_context_bound_and_prioritizes_owned_stock(
    monkeypatch: pytest.MonkeyPatch,
    catalog: MealCatalog,
    candidate: dict[str, Any],
) -> None:
    rows = groceries(candidate)
    service = RecipeService(Settings(), RecipeSettings(context_ingredients=1), clock=lambda: NOW)
    monkeypatch.setattr(MealCatalog, "load", lambda path: catalog)
    monkeypatch.setattr(
        service.cache,
        "pin",
        lambda *args: {
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
                for row in rows
            ],
        },
    )
    request = RecipeRequest(pantry={"rice": "500g"}, use_first=("rice",), retailer_ids=("tesco",))
    inputs = service.prepare(request)
    assert list(inputs["ingredient_context"]) == ["rice"]
    assert inputs["offers"] == []
    assert inputs["effective_request"] == request.model_dump(mode="json")


def test_loyalty_filters_and_seasonings_cost(
    catalog: MealCatalog, candidate: dict[str, Any]
) -> None:
    from grocery_agent.meals.planner import rejection_reason
    from grocery_agent.models.offer import Offer

    rows = groceries(candidate)
    payload = rows[0].offer.model_dump(mode="json", exclude_computed_fields=True)
    payload["promotion"] = {"kind": "loyalty", "requires_loyalty": True}
    offer = Offer.model_validate(payload)
    enabled = effective_catalog(catalog, RecipeRequest(allow_loyalty=True))
    disabled = effective_catalog(catalog, RecipeRequest(allow_loyalty=False))
    assert rejection_reason(offer, enabled.policy) is None
    assert rejection_reason(offer, disabled.policy) == "loyalty"
    charged = plan_meals(MemoryReader(rows), disabled, NOW)
    free_seasonings = plan_meals(
        MemoryReader(rows),
        effective_catalog(
            catalog,
            RecipeRequest(seasonings_available=True),
        ),
        NOW,
    )
    costs = {meal.title: meal.usage_cost_per_serving_czk for meal in charged.meals}
    for meal in free_seasonings.meals:
        assert (
            costs[meal.title] - meal.usage_cost_per_serving_czk
            == catalog.policy.seasoning_allowance_czk
        )
