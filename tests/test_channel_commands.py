from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from grocery_agent.apps import recipes
from grocery_agent.backend.recipes import effective_catalog
from grocery_agent.channel_commands import HELP_TEXT, ChannelAction, parse_channel_command
from grocery_agent.contracts import Capabilities
from grocery_agent.meals.catalog import MealCatalog
from grocery_agent.recipes import RecipeRequest, RecipeResult


@pytest.fixture
def capabilities() -> Capabilities:
    return Capabilities(
        providers=("template", "codex", "ollama"),
        default_provider="template",
        models={"codex": ("configured-codex",), "ollama": ("configured-ollama",)},
        cache_policies=("cache-only", "local", "refresh"),
        default_cache_policy="cache-only",
        sources=("kupi", "mock"),
        scope="praha",
        ingredients=("rice", "lentils", "chicken"),
        meal_styles=("main", "breakfast", "snack"),
        refresh_allowed=False,
        email_enabled=True,
        simplex_enabled=True,
    )


@pytest.mark.parametrize("command", ["recipe", "meal", "meals", "/recipe", "/meal", "/meals"])
def test_aliases_and_capability_defaults(command: str, capabilities: Capabilities) -> None:
    request = parse_channel_command(command, capabilities)
    assert isinstance(request, RecipeRequest)
    assert request.provider == "template"
    assert request.source_ids == ("kupi",)
    assert request.cache_policy == "cache-only"
    assert request.servings == 2


@pytest.mark.parametrize("prefix", ["", "/"])
def test_actions_and_uuid_normalization(prefix: str, capabilities: Capabilities) -> None:
    assert parse_channel_command(prefix + "help", capabilities) == ChannelAction(action="help")
    for action in ("verify", "confirm"):
        for size in (20, 200):
            token = "A_-0" * (size // 4)
            assert parse_channel_command(f"{prefix}{action} {token}", capabilities) == (
                ChannelAction.model_validate({"action": action, "argument": token})
            )
    for action in ("status", "cancel"):
        value = "ABCDEFAB123412341234123456789ABC"
        parsed = parse_channel_command(f"{prefix}{action} {value}", capabilities)
        assert isinstance(parsed, ChannelAction)
        assert parsed.argument == str(UUID(value))


@pytest.mark.parametrize(
    "text",
    [
        "",
        " ",
        "//help",
        "HELP",
        "scrape source=kupi",
        "help extra",
        "verify",
        "confirm",
        "status",
        "cancel",
        "status invalid",
        "cancel help extra",
        "verify " + "a" * 19,
        "confirm " + "a" * 201,
        "verify " + "a" * 20 + "=",
        "verify " + "é" * 20,
        "confirm " + "a" * 20 + "/",
        "verify " + "a" * 20 + " extra",
        'confirm "' + "a" * 20 + ' "',
        'recipe style="main',
        "recipe " + "x" * 4090,
        "help " + " ".join("x" for _ in range(64)),
    ],
)
def test_invalid_syntax(text: str, capabilities: Capabilities) -> None:
    with pytest.raises(ValueError):
        parse_channel_command(text, capabilities)


@pytest.mark.parametrize(
    "parameters",
    [
        "unknown=x",
        "cooking_time=30",
        "nutrition=protein",
        "request_id=123",
        "pantry=rice",
        "source_ids=kupi",
        "servings",
        "servings=",
        'style=" "',
        "servings=2 servings=3",
        "style=main meal_style=main",
        "exclude=rice exclusions=chicken",
        "have=rice=1g,rice=2g",
        'have="rice=1g, rice=2g"',
        "have=rice=0g",
        "have=rice=1.5",
        "have=rice=-1g",
        "have=rice=NaNg",
        "have=rice=1g,",
        "have=unknown=1g",
        "exclude=unknown",
        "exclude=rice,rice",
        "exclude=rice,",
        "servings=0",
        "servings=21",
        "servings=2.0",
        "servings=+2",
        "servings=２",
        "max_stores=0",
        "max_stores=4",
        "budget=0",
        "budget=-1",
        "budget=NaN",
        "budget=Infinity",
        "budget=not-money",
        "provider=other",
        "source=unknown",
        "cache_policy=other",
        "cache_policy=no-cache",
        "model=configured-codex",
        "provider=ollama",
        "provider=codex model=configured-ollama",
        "provider=codex model=unknown",
        "style=unknown",
    ],
)
def test_rejects_invalid_or_unsupported_parameters(
    parameters: str, capabilities: Capabilities
) -> None:
    with pytest.raises(ValueError):
        parse_channel_command("recipe " + parameters, capabilities)


def test_explicit_selections_and_quoted_stock(capabilities: Capabilities) -> None:
    request = parse_channel_command(
        "recipe provider=ollama model=configured-ollama source=mock cache_policy=local "
        'servings=20 max_stores=3 style="snack" have="rice=0.500125kg,lentils=available" '
        "budget=12.340000000000000001 exclude=chicken",
        capabilities,
    )
    assert isinstance(request, RecipeRequest)
    assert request.provider == "ollama" and request.model == "configured-ollama"
    assert request.source_ids == ("mock",) and request.cache_policy == "local"
    assert request.servings == 20 and request.max_stores == 3 and request.meal_style == "snack"
    assert request.pantry == {"rice": "0.500125kg", "lentils": "available"}
    assert request.exclusions == ("chicken",)
    assert request.max_cost_per_serving_czk == Decimal("12.340000000000000001")


def test_capabilities_constrain_defaults_and_refresh(capabilities: Capabilities) -> None:
    for update in (
        {"default_provider": None},
        {"default_provider": "codex", "providers": ("template",)},
        {"default_cache_policy": "no-cache"},
        {"sources": ()},
    ):
        changed = Capabilities.model_validate({**capabilities.model_dump(), **update})
        with pytest.raises(ValueError):
            parse_channel_command("recipe", changed)
    elevated = Capabilities.model_validate({**capabilities.model_dump(), "refresh_allowed": True})
    parsed = parse_channel_command("recipe cache_policy=refresh", elevated)
    assert isinstance(parsed, RecipeRequest) and parsed.cache_policy == "refresh"


def test_combined_commands_use_explicit_source_profiles(capabilities: Capabilities) -> None:
    supported = Capabilities.model_validate({**capabilities.model_dump(), "combined_sources": True})
    text = "recipe source=kupi,mock profiles=kupi:" + "a" * 24 + ",mock:" + "b" * 24
    request = parse_channel_command(text, supported)
    assert isinstance(request, RecipeRequest)
    assert request.source_ids == ("kupi", "mock")
    assert request.profile_fingerprints == {"kupi": "a" * 24, "mock": "b" * 24}
    with pytest.raises(ValueError, match="combined"):
        parse_channel_command(text, capabilities)
    for invalid in (
        "profiles=kupi:invalid",
        "profiles=kupi:legacy,kupi:legacy",
        "profiles=wrong:legacy",
    ):
        with pytest.raises(ValueError):
            parse_channel_command("recipe " + invalid, supported)


def test_character_boundary(capabilities: Capabilities) -> None:
    text = "recipe" + " " * (4096 - len("recipe"))
    assert isinstance(parse_channel_command(text, capabilities), RecipeRequest)
    with pytest.raises(ValueError, match="4096"):
        parse_channel_command(text + " ", capabilities)


def test_cli_request_parity_and_pantry_arithmetic(
    capabilities: Capabilities, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[RecipeRequest] = []

    def capture(request: RecipeRequest) -> RecipeResult:
        seen.append(request)
        return RecipeResult(
            request_id=request.request_id,
            title="Offline",
            ingredients=(),
            steps=(),
            servings=request.servings,
            provider=request.provider,
        )

    class Service:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def prepare(self, request: RecipeRequest) -> dict[str, Any]:
            return {}

        def execute(
            self, request: RecipeRequest, inputs: dict[str, Any]
        ) -> tuple[dict[str, Any], str]:
            return capture(request).model_dump(mode="json"), "<p>Offline</p>"

    monkeypatch.setattr(recipes, "RecipeService", Service)
    monkeypatch.setattr(recipes, "save_reports", lambda *args, **kwargs: None)
    assert (
        recipes.main(
            [
                "--provider",
                "template",
                "--source",
                "kupi",
                "--cache-policy",
                "cache-only",
                "--servings",
                "3",
                "--meal-style",
                "main",
                "--max-stores",
                "2",
                "--have",
                "rice=0.500125kg",
                "--have",
                "lentils=available",
                "--budget",
                "80.0000",
                "--exclude",
                "chicken",
            ]
        )
        == 0
    )
    capsys.readouterr()
    channel = parse_channel_command(
        "meals servings=3 meal_style=main max_stores=2 "
        "have=rice=0.500125kg,lentils=available budget=80.0000 exclusions=chicken",
        capabilities,
    )
    assert isinstance(channel, RecipeRequest)
    assert channel.model_dump(exclude={"request_id"}) == seen[0].model_dump(exclude={"request_id"})
    assert channel.max_cost_per_serving_czk is not None
    assert channel.max_cost_per_serving_czk.as_tuple().exponent == -4
    catalog = MealCatalog.load(Path(__file__).resolve().parents[1] / "config/meals.toml")
    resolved = effective_catalog(catalog, channel)
    required = Decimal("750.125")
    owned = resolved.pantry.available_grams("rice", required)
    assert owned == Decimal("500.125")
    assert required - owned == Decimal("250.000")
    assert resolved.pantry.available_grams("lentils", required) == required


def test_help_and_action_errors_do_not_echo_private_tokens(capabilities: Capabilities) -> None:
    secret = "private-token-0123456789"
    with pytest.raises(ValueError) as error:
        parse_channel_command("confirm " + secret + "=", capabilities)
    assert secret not in str(error.value) and secret not in HELP_TEXT
    assert "have=rice=500g,lentils=available" in HELP_TEXT
    assert "confirm" in HELP_TEXT and "verification" in HELP_TEXT.lower()


def test_channel_action_forbids_extra_fields() -> None:
    payload: dict[str, Any] = {"action": "help", "secret": "unused"}
    with pytest.raises(ValueError):
        ChannelAction.model_validate(payload)
