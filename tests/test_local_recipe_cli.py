"""Offline checks for the thin CLI, recipe configuration, and request reports."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine

from grocery_agent.apps import recipes
from grocery_agent.catalogue.profiles import AcquisitionProfile
from grocery_agent.catalogue.repository import SQLAlchemyCatalogueRepository
from grocery_agent.config import Settings
from grocery_agent.contracts import Capabilities
from grocery_agent.http_client import GroceryClient
from grocery_agent.persistence.repository import SQLAlchemyOfferRepository
from grocery_agent.persistence.snapshots import FileSnapshotStore
from grocery_agent.recipe_config import RecipeSettings
from grocery_agent.recipe_service import RecipeService
from grocery_agent.recipes import RecipeRequest
from tests.application_support import NOW, complete_batch
from tests.test_meals import groceries

FINGERPRINT = "abcdef0123456789abcdef01"


@pytest.fixture
def local_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> list[tuple[Settings, RecipeSettings, RecipeRequest]]:
    calls: list[tuple[Settings, RecipeSettings, RecipeRequest]] = []

    class Service:
        def __init__(self, settings: Settings, options: RecipeSettings) -> None:
            self.settings, self.options = settings, options

        def prepare(self, request: RecipeRequest) -> dict[str, Any]:
            calls.append((self.settings, self.options, request))
            return {
                "catalogue_snapshot": {"run_id": "pinned-run"},
                "collections": [{"source_id": "kupi", "run_id": "pinned-run"}],
                "effective_request": request.model_dump(mode="json"),
            }

        def execute(
            self, request: RecipeRequest, inputs: dict[str, Any]
        ) -> tuple[dict[str, Any], str]:
            assert inputs["effective_request"] == request.model_dump(mode="json")
            return {
                "request_id": str(request.request_id),
                "status": "succeeded",
                "run_id": "pinned-run",
                "profile_fingerprints": request.profile_fingerprints,
            }, "<html><body>Recipe česky</body></html>"

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GROCERY_REPORT_DIR", str(tmp_path / "reports"))
    monkeypatch.setattr(recipes, "RecipeService", Service)
    return calls


def test_local_defaults_and_immutable_reports(
    local_service: Any, capsys: Any, tmp_path: Path
) -> None:
    assert recipes.main([]) == 0
    settings, options, request = local_service[0]
    assert request.provider == options.default_provider == "codex"
    assert request.cache_policy == options.default_cache_policy == "local"
    result = json.loads(capsys.readouterr().out)
    directory = settings.report_dir / "requests" / str(request.request_id)
    assert json.loads((directory / "report.json").read_text()) == result
    assert json.loads((directory / "request.json").read_text()) == request.model_dump(mode="json")
    inputs = json.loads((directory / "inputs.json").read_text())
    assert inputs["effective_request"] == request.model_dump(mode="json")
    assert inputs["catalogue_snapshot"] == {"run_id": "pinned-run"}
    assert inputs["collections"] == [{"source_id": "kupi", "run_id": "pinned-run"}]
    assert "česky" in (directory / "report.html").read_text()
    before = (directory / "report.json").read_bytes()
    with pytest.raises(FileExistsError, match="already exists"):
        recipes.save_reports(settings, request, {"changed": True}, "changed")
    assert (directory / "report.json").read_bytes() == before
    assert not list(directory.parent.glob(".report-*"))
    assert not (tmp_path / "reports" / "report.json").exists()


def test_local_options_mapped_profiles_and_output_copies(
    local_service: Any, tmp_path: Path, capsys: Any
) -> None:
    path = tmp_path / "recipes.toml"
    path.write_text('default_provider = "template"\nmodel = "config-model"\n')
    json_path, html_path = tmp_path / "export.json", tmp_path / "export.html"
    assert (
        recipes.main(
            [
                "--config",
                str(path),
                "--source",
                "kupi",
                "--source",
                "mock",
                "--profile",
                f"kupi={FINGERPRINT}",
                "--profile",
                "mock=legacy",
                "--cache-only",
                "--have",
                "rice=500g",
                "--budget",
                "80.0000",
                "--output-json",
                str(json_path),
                "--output-html",
                str(html_path),
            ]
        )
        == 0
    )
    settings, options, request = local_service[0]
    assert options.model == request.model == "config-model"
    assert request.provider == "template"
    assert request.cache_policy == "cache-only"
    assert request.source_ids == ("kupi", "mock")
    assert request.profile_fingerprints == {"kupi": FINGERPRINT, "mock": "legacy"}
    assert request.pantry == {"rice": "500g"}
    directory = settings.report_dir / "requests" / str(request.request_id)
    assert json.loads(json_path.read_text()) == json.loads(capsys.readouterr().out)
    assert html_path.read_bytes() == (directory / "report.html").read_bytes()


def test_default_config_path_is_loaded(local_service: Any, tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "recipes.toml").write_text(
        'default_provider = "template"\ndefault_cache_policy = "cache-only"\n'
    )
    assert recipes.main([]) == 0
    assert local_service[0][2].provider == "template"
    assert local_service[0][2].cache_policy == "cache-only"


def test_environment_overrides_toml_and_cli_overrides_environment(
    local_service: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "recipes.toml"
    path.write_text(
        'default_provider = "template"\ndefault_cache_policy = "cache-only"\n'
        'model = "file-model"\ncontext_ingredients = 12\nprovider_timeout_seconds = 30\n'
        '[source_scopes]\nkupi = ["kupi:locality:praha"]\n'
        '[[acquisition_profiles]]\nsource_id = "kupi"\nname = "produce"\n'
        'categories = ["produce"]\n'
    )
    monkeypatch.setenv("GROCERY_RECIPES_DEFAULT_PROVIDER", "ollama")
    monkeypatch.setenv("GROCERY_RECIPES_DEFAULT_CACHE_POLICY", "no-cache")
    monkeypatch.setenv("GROCERY_RECIPES_MODEL", "env-model")
    monkeypatch.setenv("GROCERY_RECIPES_CONTEXT_INGREDIENTS", "24")
    monkeypatch.setenv(
        "GROCERY_RECIPES_SOURCE_SCOPES",
        '{"kupi":["kupi:locality:brno"],"mock":["mock:local"]}',
    )
    options = RecipeSettings.load(path)
    assert options.default_provider == "ollama"
    assert options.default_cache_policy == "no-cache"
    assert options.model == "env-model"
    assert options.context_ingredients == 24
    assert options.provider_timeout_seconds == 30
    assert options.source_scopes == {"kupi": ("kupi:locality:brno",), "mock": ("mock:local",)}
    assert options.acquisition_profiles == (
        AcquisitionProfile(source_id="kupi", name="produce", categories=("produce",)),
    )
    assert recipes.main(["--config", str(path)]) == 0
    assert local_service[0][2].provider == "ollama"
    assert local_service[0][2].model == "env-model"
    assert local_service[0][2].cache_policy == "no-cache"
    assert (
        recipes.main(
            [
                "--config",
                str(path),
                "--provider",
                "template",
                "--model",
                "cli-model",
                "--cache-only",
            ]
        )
        == 0
    )
    assert local_service[1][2].provider == "template"
    assert local_service[1][2].model == "cli-model"
    assert local_service[1][2].cache_policy == "cache-only"


@pytest.mark.parametrize(
    "arguments",
    [
        ["--cache-only", "--no-cache"],
        ["--cache-policy", "refresh", "--cache-only"],
        ["--cache-policy", "local", "--no-cache"],
        ["--no-wait"],
    ],
)
def test_argument_conflicts_fail_before_service(local_service: Any, arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        recipes.main(arguments)
    assert error.value.code == 2
    assert local_service == []


@pytest.mark.parametrize(
    "arguments",
    [
        ["--profile", "legacy"],
        ["--profile", "kupi="],
        ["--profile", "=legacy"],
        ["--profile", "kupi=legacy", "--profile", "kupi=legacy"],
        ["--profile", "mock=legacy"],
        ["--profile", "kupi=INVALID"],
        ["--profile", "kupi=legacy", "--profile-fingerprint", "legacy"],
        ["--source", "kupi", "--source", "kupi"],
    ],
)
def test_invalid_selections_fail_before_service(local_service: Any, arguments: list[str]) -> None:
    assert recipes.main(arguments) == 1
    assert local_service == []


@pytest.mark.parametrize(
    "contents",
    [
        "context_ingredients = 0",
        "provider_timeout_seconds = 0",
        'default_provider = "unknown"',
        'default_cache_policy = "unknown"',
        "unknown_option = true",
    ],
)
def test_invalid_or_missing_config_is_reported(
    local_service: Any, tmp_path: Path, contents: str, capsys: Any
) -> None:
    path = tmp_path / "invalid.toml"
    assert recipes.main(["--config", str(path)]) == 1
    path.write_text(contents)
    assert recipes.main(["--config", str(path)]) == 1
    assert local_service == []
    assert capsys.readouterr().err


def test_output_cannot_replace_immutable_reports(local_service: Any, tmp_path: Path) -> None:
    settings = Settings()
    request = RecipeRequest()
    with pytest.raises(ValueError, match="outside immutable"):
        recipes.save_reports(
            settings, request, {}, "html", tmp_path / "reports/requests/other/report.json"
        )
    with pytest.raises(ValueError, match="must differ"):
        recipes.save_reports(settings, request, {}, "html", tmp_path / "same", tmp_path / "same")
    assert not (settings.report_dir / "requests").exists()


def test_cli_matches_shared_service_and_saved_inputs_replay_offline(
    engine: Engine,
    repository: SQLAlchemyOfferRepository,
    snapshots: FileSnapshotStore,
    candidate: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: Any,
) -> None:
    root = Path(__file__).resolve().parents[1]
    run = complete_batch(
        repository, snapshots, [item.offer for item in groceries(candidate)], source="kupi"
    )
    SQLAlchemyCatalogueRepository(engine).publish(run.run_id)
    settings = Settings(
        database_url=str(engine.url),
        meal_config=root / "config/meals.toml",
        report_dir=tmp_path / "reports",
        _env_file=None,
    )

    class Provider:
        def generate(self, request: RecipeRequest, context: str) -> dict[str, Any]:
            assert "chicken" in json.loads(context)
            return {
                "title": "Offline chicken",
                "minutes": 20,
                "ingredients": [{"ingredient": "chicken", "quantity": "800", "unit": "g"}],
                "steps": ["Cook the chicken thoroughly."],
            }

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("cache-only execution must not acquire or reread the database")

    service = RecipeService(settings, RecipeSettings(), lambda: NOW, Provider(), refresh=forbidden)
    monkeypatch.setattr(recipes, "Settings", lambda: settings)
    monkeypatch.setattr(recipes, "RecipeService", lambda settings, options: service)
    assert recipes.main(["--cache-only", "--have", "chicken=100g"]) == 0
    result = json.loads(capsys.readouterr().out)
    directory = settings.report_dir / "requests" / result["request_id"]
    request = RecipeRequest.model_validate_json((directory / "request.json").read_text())
    inputs = json.loads((directory / "inputs.json").read_text())
    assert inputs["run_id"] == run.run_id
    assert inputs["profile_fingerprint"] == "legacy"
    monkeypatch.setattr("grocery_agent.recipe_cache.create_database_engine", forbidden)
    replayed, html = service.execute(request, inputs)
    assert replayed == result
    assert html == (directory / "report.html").read_text()
    assert result["report"]["meals"][0]["usage_cost_per_serving_czk"] == "38.00"


@pytest.mark.parametrize("mapped", [False, True])
def test_local_and_remote_request_parity_with_capability_defaults(
    local_service: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any, mapped: bool
) -> None:
    supported = Capabilities(
        providers=("template",),
        default_provider="template",
        models={},
        cache_policies=("cache-only",),
        default_cache_policy="cache-only",
        sources=("kupi",),
        scope="praha",
        ingredients=("rice",),
        meal_styles=("main",),
        refresh_allowed=False,
        email_enabled=False,
        simplex_enabled=False,
    )
    bodies: list[dict[str, Any]] = []
    paths: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/v1/capabilities":
            return httpx.Response(200, json=supported.model_dump(mode="json"))
        assert request.url.path == "/v1/recipes"
        bodies.append(json.loads(request.content))
        return httpx.Response(
            202,
            json={
                "job_id": "job",
                "kind": "recipe",
                "status": "queued",
                "phase": "queued",
                "attempts": 0,
                "created_at": NOW.isoformat(),
                "updated_at": NOW.isoformat(),
                "error_code": None,
            },
        )

    def client(url: str, token: str) -> GroceryClient:
        return GroceryClient(url, token, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(recipes, "GroceryClient", client)
    monkeypatch.setenv("GROCERY_API_TOKEN", "test-session")
    # Local options must not change the backend's capability defaults.
    monkeypatch.setenv("GROCERY_RECIPES_DEFAULT_PROVIDER", "ollama")
    monkeypatch.setenv("GROCERY_RECIPES_DEFAULT_CACHE_POLICY", "refresh")
    common = ["--have", "rice=500g", "--budget", "80.0000"]
    if mapped:
        common += [
            "--source",
            "kupi",
            "--source",
            "mock",
            "--profile",
            "kupi=legacy",
            "--profile",
            f"mock={FINGERPRINT}",
        ]
    else:
        common += ["--profile-fingerprint", FINGERPRINT]
    assert recipes.main([*common, "--provider", "template", "--cache-only"]) == 0
    capsys.readouterr()
    assert recipes.main([*common, "--api-url", "http://backend.test", "--no-wait"]) == 0
    assert paths == ["/v1/capabilities", "/v1/recipes"]
    remote = RecipeRequest.model_validate(bodies[0])
    assert remote.model_dump(mode="json", exclude={"request_id"}) == local_service[0][2].model_dump(
        mode="json", exclude={"request_id"}
    )
    assert json.loads(capsys.readouterr().out)["status"] == "queued"


def test_remote_wait_exports_backend_report(
    local_service: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    paths: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/v1/capabilities":
            return httpx.Response(
                200,
                json=Capabilities(
                    providers=("template",),
                    default_provider="template",
                    models={},
                    cache_policies=("cache-only",),
                    default_cache_policy="cache-only",
                    sources=("kupi",),
                    scope="praha",
                    ingredients=("rice",),
                    meal_styles=("main",),
                    refresh_allowed=False,
                    email_enabled=False,
                    simplex_enabled=False,
                ).model_dump(mode="json"),
            )
        if request.url.path == "/v1/jobs/job/result":
            return httpx.Response(200, json={"status": "ok", "run_id": "remote-run"})
        if request.url.path == "/v1/jobs/job/report":
            return httpx.Response(200, text="<html>Backend report</html>")
        assert request.url.path in {"/v1/recipes", "/v1/jobs/job"}
        return httpx.Response(
            202 if request.method == "POST" else 200,
            json={
                "job_id": "job",
                "kind": "recipe",
                "status": "queued" if request.method == "POST" else "succeeded",
                "phase": "queued" if request.method == "POST" else "succeeded",
                "attempts": 0,
                "created_at": NOW.isoformat(),
                "updated_at": NOW.isoformat(),
                "error_code": None,
            },
        )

    def client(url: str, token: str) -> GroceryClient:
        return GroceryClient(url, token, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(recipes, "GroceryClient", client)
    monkeypatch.setenv("GROCERY_API_TOKEN", "test-session")
    output_json, output_html = tmp_path / "remote.json", tmp_path / "remote.html"
    assert (
        recipes.main(
            [
                "--api-url",
                "http://backend.test",
                "--output-json",
                str(output_json),
                "--output-html",
                str(output_html),
            ]
        )
        == 0
    )
    assert local_service == []
    assert json.loads(output_json.read_text()) == json.loads(capsys.readouterr().out)
    assert output_html.read_text() == "<html>Backend report</html>"
    assert paths == [
        "/v1/capabilities",
        "/v1/recipes",
        "/v1/jobs/job",
        "/v1/jobs/job/result",
        "/v1/jobs/job/report",
    ]
