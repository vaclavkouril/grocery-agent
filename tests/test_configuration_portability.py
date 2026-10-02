"""Offline checks for shared layering and CWD-independent configuration defaults."""

from pathlib import Path

import pytest

from grocery_agent import configuration
from grocery_agent.apps import collect, email, scrape, simplex
from grocery_agent.channels.config import EmailConfig, SimplexSettings
from grocery_agent.collector.config import CollectSettings, ScrapeSettings
from grocery_agent.config import Settings
from grocery_agent.recipe_config import RecipeSettings


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GROCERY_SHARED_CONFIG", raising=False)
    return tmp_path


def test_checkout_defaults_from_arbitrary_cwd(isolated):
    settings = Settings(_env_file=None)
    assert settings.meal_config == configuration.SOURCE_CONFIG_ROOT / "meals.toml"
    assert settings.meal_config.is_file()
    assert settings.collector_config.is_absolute()
    assert configuration.default_config("recipes").is_file()
    assert RecipeSettings.load().default_provider == "codex"
    assert list(isolated.iterdir()) == []


def test_packaged_defaults_before_checkout_and_local_app_override(isolated, monkeypatch):
    package = isolated / "installed" / "grocery_agent"
    defaults = package / "defaults"
    defaults.mkdir(parents=True)
    (defaults / "meals.toml").write_text("# packaged meals\n")
    (defaults / "recipes.toml").write_text('default_provider = "template"\n')
    monkeypatch.setattr(configuration, "PACKAGE_ROOT", package)
    monkeypatch.setattr(configuration, "SOURCE_CONFIG_ROOT", isolated / "missing-checkout")
    assert Settings(_env_file=None).meal_config == defaults / "meals.toml"
    assert RecipeSettings.load().default_provider == "template"
    local = isolated / "config"
    local.mkdir()
    (local / "recipes.toml").write_text('default_provider = "ollama"\n')
    assert configuration.default_config("recipes") == local / "recipes.toml"
    assert RecipeSettings.load().default_provider == "ollama"


def test_shared_global_paths_environment_and_explicit_precedence(isolated, monkeypatch):
    directory = isolated / "deployment"
    directory.mkdir()
    shared = directory / "shared.toml"
    shared.write_text(
        '[shared]\nhttp_timeout_seconds = 12\nlog_level = "DEBUG"\n'
        'snapshot_dir = "snapshots"\nmeal_config = "meals.toml"\n'
        'collector_config = "collector.toml"\ndatabase_url = "sqlite:///db/offers.db?timeout=10"\n'
    )
    monkeypatch.setenv("GROCERY_SHARED_CONFIG", str(shared))
    before = set(directory.iterdir())
    settings = Settings(_env_file=None)
    assert settings.http_timeout_seconds == 12
    assert settings.snapshot_dir == directory / "snapshots"
    assert settings.meal_config == directory / "meals.toml"
    assert settings.collector_config == directory / "collector.toml"
    assert settings.report_dir == directory / "data/reports"
    assert settings.lock_path == directory / "data/workflow.lock"
    assert settings.database_url == f"sqlite:///{directory}/db/offers.db?timeout=10"
    monkeypatch.setenv("GROCERY_HTTP_TIMEOUT_SECONDS", "20")
    monkeypatch.setenv("GROCERY_REPORT_DIR", "env-reports")
    assert Settings(_env_file=None).http_timeout_seconds == 20
    assert Settings(_env_file=None).report_dir == Path("env-reports")
    assert Settings(http_timeout_seconds=25, _env_file=None).http_timeout_seconds == 25
    assert set(directory.iterdir()) == before


def test_explicit_shared_path_does_not_mutate_environment(isolated, monkeypatch):
    env_path = isolated / "env.toml"
    cli_path = isolated / "cli.toml"
    env_path.write_text("[shared]\nhttp_timeout_seconds = 11\n")
    cli_path.write_text("[shared]\nhttp_timeout_seconds = 22\n")
    monkeypatch.setenv("GROCERY_SHARED_CONFIG", str(env_path))
    assert Settings.load(shared_config=cli_path, _env_file=None).http_timeout_seconds == 22
    assert Settings(_env_file=None).http_timeout_seconds == 11


def test_shared_context_survives_nested_load_and_restores_after_error(isolated):
    shared = isolated / "shared.toml"
    shared.write_text("[shared]\nhttp_timeout_seconds = 22\n[recipes]\ncontext_ingredients = 12\n")
    app = isolated / "empty.toml"
    app.write_text("")
    with pytest.raises(FileNotFoundError):
        with configuration.shared_config_context(shared):
            assert Settings.load(_env_file=None).http_timeout_seconds == 22
            assert RecipeSettings.load(app).context_ingredients == 12
            assert configuration.shared_config_path() == shared
            RecipeSettings.load(isolated / "missing.toml")
    assert configuration.shared_config_path() is None


@pytest.mark.parametrize(
    "text",
    [
        "[unknown]\nvalue = 1\n",
        "[shared]\nunknown = 1\n",
        "[recipes]\nunknown = 1\n",
        "[backend]\nunknown = 1\n",
        '[email]\nservice_token = "PRIVATE"\n',
        '[backend]\nemail_transport_token = "PRIVATE"\n',
        '[shared]\ndatabase_url = "postgresql://user:PRIVATE@localhost/db"\n',
        '[scrape]\n[[scrape.profiles]]\nsource_id = "kupi"\npassword = "PRIVATE"\n',
        'shared = "invalid"\n',
    ],
)
def test_shared_rejects_unknown_fields_and_credentials(isolated, text):
    path = isolated / "shared.toml"
    path.write_text(text)
    with pytest.raises(ValueError) as error:
        Settings.load(shared_config=path, _env_file=None)
    assert "PRIVATE" not in str(error.value)


@pytest.mark.parametrize(
    "name, cls, field, shared_value, app_value, env_value, prefix",
    [
        ("recipes", RecipeSettings, "context_ingredients", 10, 20, "30", "RECIPES"),
        ("collect", CollectSettings, "daily_at", "01:00", "02:00", "03:00", "COLLECT"),
        ("email", EmailConfig, "batch_size", 10, 20, "30", "EMAIL"),
        ("simplex", SimplexSettings, "history_count", 10, 20, "30", "SIMPLEX"),
    ],
)
def test_shared_app_and_environment_layers(
    isolated, monkeypatch, name, cls, field, shared_value, app_value, env_value, prefix
):
    import json

    shared = isolated / "shared.toml"
    shared.write_text(f"[{name}]\n{field} = {json.dumps(shared_value)}\n")
    app = isolated / "app.toml"
    app.write_text("")
    monkeypatch.setenv("GROCERY_SHARED_CONFIG", str(shared))
    assert getattr(cls.load(app), field) == shared_value
    app.write_text(f"{field} = {json.dumps(app_value)}\n")
    assert getattr(cls.load(app), field) == app_value
    monkeypatch.setenv(f"GROCERY_{prefix}_{field.upper()}", env_value)
    loaded = cls.load(app)
    assert str(getattr(loaded, field)) == env_value


def test_scrape_profile_layers(isolated, monkeypatch):
    shared = isolated / "shared.toml"
    shared.write_text('[[scrape.profiles]]\nsource_id = "mock"\nname = "shared"\n')
    app = isolated / "app.toml"
    app.write_text("")
    assert ScrapeSettings.load(app, shared_config=shared).profiles[0].name == "shared"
    monkeypatch.setenv("GROCERY_SCRAPE_PROFILES", '[{"source_id":"mock","name":"env"}]')
    assert ScrapeSettings.load(app, shared_config=shared).profiles[0].name == "env"


def test_collect_cli_overrides_environment_and_shared_config(isolated, monkeypatch):
    shared = isolated / "shared.toml"
    shared.write_text('[shared]\nhttp_timeout_seconds = 11\n[collect]\ndaily_at = "01:00"\n')
    app = isolated / "app.toml"
    app.write_text('daily_at = "02:00"\n[[profiles]]\nsource_id = "mock"\n')
    monkeypatch.setenv("GROCERY_COLLECT_DAILY_AT", "03:00")
    seen = []

    def factory(settings, options):
        seen.append((settings, options))
        raise ValueError("stop before IO")

    monkeypatch.setattr(collect, "create_service", factory)
    monkeypatch.setattr(collect, "configure_logging", lambda level: None)
    assert (
        collect.main(
            [
                "--config",
                str(app),
                "--shared-config",
                str(shared),
                "--daily-at",
                "04:00",
                "--timezone",
                "UTC",
                "--once",
            ]
        )
        == 1
    )
    settings, options = seen[0]
    assert settings.http_timeout_seconds == 11
    assert options.daily_at == "04:00" and options.timezone == "UTC"
    assert not (isolated / "data").exists()


def test_channel_entrypoints_use_shared_loader(isolated, monkeypatch):
    shared = isolated / "shared.toml"
    shared.write_text("[email]\nbatch_size = 7\n[simplex]\nhistory_count = 8\n")
    app = isolated / "empty.toml"
    app.write_text("")
    seen = []
    original = email.EmailConfig.load

    def load(path, **kwargs):
        config = original(path, **kwargs)
        seen.append(config)
        return config

    monkeypatch.setattr(email.EmailConfig, "load", load)
    assert email.main(["--config", str(app), "--shared-config", str(shared), "--once"]) == 0
    assert seen[0].batch_size == 7
    monkeypatch.setattr(simplex, "run", lambda options, **kwargs: seen.append(options) or 0)
    assert simplex.main(["--config", str(app), "--shared-config", str(shared), "--once"]) == 0
    assert seen[1].history_count == 8


def test_relative_backend_paths_respect_their_toml_directory(isolated):
    shared = isolated / "shared.toml"
    shared.write_text('[backend]\nfrontend_dir = "site"\n')
    assert configuration.shared_defaults("backend", shared)["frontend_dir"] == isolated / "site"
    directory = isolated / "application"
    directory.mkdir()
    app = directory / "backend.toml"
    app.write_text('frontend_dir = "external"\n')
    assert (
        configuration.app_defaults("backend", app, shared_config=shared)["frontend_dir"]
        == directory / "external"
    )
    assert configuration.app_defaults("backend", app)["frontend_dir"] == directory / "external"


def test_backend_shared_schema_matches_all_nonsecret_fields(isolated):
    from grocery_agent.backend.config import BackendSettings

    credentials = {"email_transport_token", "simplex_transport_token"}
    assert configuration._SCHEMAS["backend"] == BackendSettings.model_fields.keys() - credentials
    shared = isolated / "shared.toml"
    shared.write_text(
        "[backend]\npassword_login_enabled = true\ncookie_sessions_enabled = true\n"
        'cookie_secure = false\ntrusted_origin = "http://localhost"\nauth_rate_limit = 12\n'
        "auth_rate_window_seconds = 120\nrefresh_cooldown_seconds = 60\n"
    )
    values = configuration.shared_defaults("backend", shared)
    assert values["password_login_enabled"] is True
    assert values["refresh_cooldown_seconds"] == 60


@pytest.mark.parametrize("enabled", [False, True])
def test_backend_password_policy_shared_and_default_app_load(isolated, monkeypatch, enabled):
    from grocery_agent.backend.config import BackendSettings

    monkeypatch.delenv("GROCERY_BACKEND_PASSWORD_LOGIN_ENABLED", raising=False)
    # Exercise the shipped sample from an unrelated working directory.
    assert BackendSettings.load().password_login_enabled is False
    shared = isolated / "shared.toml"
    shared.write_text(f"[backend]\npassword_login_enabled = {str(enabled).lower()}\n")
    app_directory = isolated / "config"
    app_directory.mkdir()
    app = app_directory / "backend.toml"
    app.write_text("")
    monkeypatch.setenv("GROCERY_SHARED_CONFIG", str(shared))
    assert BackendSettings.load().password_login_enabled is enabled
    assert BackendSettings.load(app).password_login_enabled is enabled
    app.write_text(f"password_login_enabled = {str(not enabled).lower()}\n")
    assert BackendSettings.load().password_login_enabled is not enabled


@pytest.mark.parametrize(
    "entry",
    [
        'password = "PRIVATE"',
        'password_login_enabled = "PRIVATE"',
        "password_login_enabled = 0",
        'password_login_enabled = ["PRIVATE"]',
    ],
)
def test_backend_password_policy_cannot_carry_credentials(isolated, monkeypatch, entry):
    from grocery_agent.backend.config import BackendSettings

    app = isolated / "backend.toml"
    app.write_text(entry + "\n")
    with pytest.raises(ValueError) as error:
        BackendSettings.load(app)
    assert "PRIVATE" not in str(error.value)
    shared = isolated / "shared.toml"
    shared.write_text("[backend]\n" + entry + "\n")
    app.write_text("")
    monkeypatch.setenv("GROCERY_SHARED_CONFIG", str(shared))
    with pytest.raises(ValueError) as error:
        BackendSettings.load(app)
    assert "PRIVATE" not in str(error.value)


def test_missing_explicit_config_is_an_error(isolated):
    with pytest.raises(FileNotFoundError):
        RecipeSettings.load(isolated / "missing.toml")
    with pytest.raises(FileNotFoundError):
        Settings.load(shared_config=isolated / "missing.toml")


def test_unknown_app_message_is_compatible(isolated):
    path = isolated / "scrape.toml"
    path.write_text("unknown = true\n")
    with pytest.raises(ValueError, match="unknown acquisition app settings"):
        ScrapeSettings.load(path)


def test_scrape_cli_shared_settings(isolated, monkeypatch):
    shared = isolated / "shared.toml"
    shared.write_text("[shared]\nhttp_timeout_seconds = 13\n")
    seen = []

    def factory(settings, options):
        seen.append(settings)
        raise ValueError("stop before IO")

    monkeypatch.setattr(scrape, "create_service", factory)
    monkeypatch.setattr(scrape, "configure_logging", lambda level: None)
    assert scrape.main(["--shared-config", str(shared), "--source", "mock"]) == 1
    assert seen[0].http_timeout_seconds == 13
