"""Visible explicit model choices with the real API and no provider execution."""

import shutil
import socket
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
import uvicorn

from grocery_agent.backend.api import create_app
from grocery_agent.backend.config import BackendSettings
from grocery_agent.config import Settings
from grocery_agent.model_choices import BENCHMARKED_LOCAL_MODELS, TESTED_CODEX_MODELS
from grocery_agent.persistence.database import create_database_engine
from grocery_agent.persistence.migrations import upgrade_database
from tests.application_support import NOW

playwright = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.browser
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def model_site(engine: Any, tmp_path: Path) -> Any:
    control = create_database_engine(f"sqlite:///{tmp_path / 'model-control.db'}")
    upgrade_database(control, "control")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    origin = f"http://localhost:{sock.getsockname()[1]}"
    settings = Settings(
        database_url=str(engine.url), meal_config=ROOT / "config/meals.toml", _env_file=None
    )
    backend = BackendSettings(
        providers=("codex", "ollama", "template"),
        registration_enabled=True,
        password_login_enabled=True,
        cookie_sessions_enabled=True,
        cookie_secure=False,
        trusted_origin=origin,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(settings, backend, engine=control, clock=lambda: NOW),
            log_level="error",
            access_log=False,
        )
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        yield origin
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        control.dispose()


def response_to(path: str, method: str = "GET") -> Any:
    return lambda response: (
        urlparse(response.url).path == path and response.request.method == method
    )


@pytest.mark.parametrize(("locale", "width"), [("cs-CZ", 390), ("en-GB", 1440)])
def test_visible_models_preserve_choices_in_languages_presets_and_jobs(
    model_site: str, locale: str, width: int, tmp_path: Path
) -> None:
    executable = shutil.which("chromium")
    if not executable:
        pytest.skip("Chromium is required")
    origin = model_site
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(executable_path=executable, headless=True)
        context = browser.new_context(locale=locale, viewport={"width": width, "height": 1000})
        context.route(
            "**/*",
            lambda route: (
                route.continue_()
                if urlparse(route.request.url).hostname in {"localhost", "127.0.0.1"}
                else route.abort()
            ),
        )
        page = context.new_page()
        page.set_default_timeout(10000)
        errors: list[str] = []
        recipe_requests: list[Any] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "request",
            lambda request: (
                recipe_requests.append(request.post_data_json)
                if urlparse(request.url).path == "/v1/recipes" and request.method == "POST"
                else None
            ),
        )
        try:
            page.goto(origin)
            page.locator("#auth-register-tab").click()
            page.locator("#register-username").fill("modeluser")
            for field in ["register-password", "register-password-confirm"]:
                page.locator(f"#{field}").fill("a long model fixture password")
            page.locator("#register-form button").click()
            playwright.expect(page.locator("#workspace")).to_be_visible()
            playwright.expect(page.locator("#register-form")).not_to_have_attribute(
                "aria-busy", "true"
            )
            for field in ["provider", "model"]:
                playwright.expect(page.locator(f"#{field}")).to_be_visible()
                assert page.locator(f"#{field}").evaluate("el => !el.closest('details')")
            playwright.expect(page.locator("#model-choice-note")).to_contain_text(
                "experimentálních" if locale.startswith("cs") else "experimental"
            )
            caps = page.request.get(f"{origin}/v1/capabilities").json()
            assert caps["models"]["codex"] == list(TESTED_CODEX_MODELS)
            assert caps["models"]["ollama"] == list(BENCHMARKED_LOCAL_MODELS)
            playwright.expect(page.locator("#model")).to_have_value(caps["defaults"]["model"])
            assert page.locator("#model option").evaluate_all(
                "options => options.map(option => option.value)"
            ) == list(TESTED_CODEX_MODELS)
            assert page.locator('#model option[value=""]').count() == 0
            page.locator("#model").select_option("gpt-6-luna")
            for language in ["en", "cs"]:
                if page.locator("#ui-language").input_value() != language:
                    with page.expect_response(response_to("/v1/me/settings", "PATCH")):
                        page.locator("#ui-language").select_option(language)
                playwright.expect(page.locator("#model")).to_have_value("gpt-6-luna")
            page.locator("#quick-preset-name").fill("Codex Luna")
            with page.expect_response(response_to("/v1/me/presets", "POST")) as saved_codex:
                page.locator("#quick-preset-name").press("Enter")
            codex_preset = saved_codex.value.json()
            assert codex_preset["parameters"]["model"] == "gpt-6-luna"
            playwright.expect(page.locator("#quick-save-preset")).to_be_enabled()

            page.locator("#provider").select_option("ollama")
            assert page.locator("#model option").evaluate_all(
                "options => options.map(option => option.value)"
            ) == list(BENCHMARKED_LOCAL_MODELS)
            for model in BENCHMARKED_LOCAL_MODELS:
                page.locator("#model").select_option(model)
                playwright.expect(page.locator("#model")).to_have_value(model)
            weak_model = "codegemma:2b"
            playwright.expect(page.locator("#model")).to_have_value(weak_model)
            for language in ["en", "cs"]:
                with page.expect_response(response_to("/v1/me/settings", "PATCH")):
                    page.locator("#ui-language").select_option(language)
                playwright.expect(page.locator("#model")).to_have_value(weak_model)
            page.locator("#quick-preset-name").fill("Experimental local")
            with page.expect_response(response_to("/v1/me/presets", "POST")) as saved_local:
                page.locator("#quick-preset-name").press("Enter")
            local_preset = saved_local.value.json()
            assert local_preset["parameters"]["provider"] == "ollama"
            assert local_preset["parameters"]["model"] == weak_model
            playwright.expect(page.locator("#quick-save-preset")).to_be_enabled()
            assert not recipe_requests
            assert page.request.get(f"{origin}/v1/jobs").json()["total"] == 0

            page.locator("#provider").select_option("codex")
            playwright.expect(page.locator("#model")).to_have_value("gpt-6-luna")
            page.locator("#provider").select_option("ollama")
            playwright.expect(page.locator("#model")).to_have_value(weak_model)
            page.locator("#preset-select").select_option(codex_preset["id"])
            page.locator("#apply-preset").click()
            playwright.expect(page.locator("#provider")).to_have_value("codex")
            playwright.expect(page.locator("#model")).to_have_value("gpt-6-luna")
            page.locator("#preset-select").select_option(local_preset["id"])
            page.locator("#apply-preset").click()
            playwright.expect(page.locator("#provider")).to_have_value("ollama")
            playwright.expect(page.locator("#model")).to_have_value(weak_model)
            page.screenshot(path=str(tmp_path / "visible-models.png"), full_page=True)

            with page.expect_response(response_to("/v1/recipes", "POST")) as queued_local:
                page.locator("#generate").click()
            assert queued_local.value.status == 202
            playwright.expect(page.locator("#generate")).to_be_enabled()
            playwright.expect(page.locator("#model")).to_have_value(weak_model)
            local_job = queued_local.value.json()["job_id"]
            original = page.request.get(f"{origin}/v1/jobs/{local_job}/request").json()
            assert original["provider"] == "ollama" and original["model"] == weak_model
            page.locator("#provider").select_option("codex")
            playwright.expect(page.locator("#model")).to_have_value("gpt-6-luna")
            with page.expect_response(response_to("/v1/recipes", "POST")) as queued_codex:
                page.locator("#generate").click()
            assert queued_codex.value.status == 202
            playwright.expect(page.locator("#generate")).to_be_enabled()
            playwright.expect(page.locator("#model")).to_have_value("gpt-6-luna")
            assert recipe_requests[-1]["model"] == "gpt-6-luna"
            assert page.request.get(f"{origin}/v1/jobs/{local_job}/request").json() == original
            assert page.request.get(f"{origin}/v1/jobs/{local_job}").json()["status"] == "queued"
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert not errors
        finally:
            context.close()
            browser.close()
