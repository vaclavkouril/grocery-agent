"""Bilingual cuisine shortcuts and easy preset creation, with no model execution."""

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
from grocery_agent.backend.repository import ControlRepository
from grocery_agent.config import Settings
from grocery_agent.cuisines import CUISINE_IDS, CUISINE_LABELS
from grocery_agent.model_choices import BENCHMARKED_LOCAL_MODELS, TESTED_CODEX_MODELS
from grocery_agent.persistence.database import create_database_engine
from grocery_agent.persistence.migrations import upgrade_database
from tests.application_support import NOW

playwright = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.browser
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cuisine_site(engine: Any, tmp_path: Path) -> Any:
    control = create_database_engine(f"sqlite:///{tmp_path / 'cuisine-control.db'}")
    upgrade_database(control, "control")
    repository = ControlRepository(control)
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
        yield origin, repository
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        control.dispose()


def response_to(path: str, method: str = "GET") -> Any:
    return lambda response: (
        urlparse(response.url).path == path and response.request.method == method
    )


def navigate(page: Any, screen: str) -> None:
    page.locator(f'.tabs a[href="#{screen}"]').click()
    playwright.expect(
        page.locator("#recipe-screen" if screen == "recipes" else f"#{screen}")
    ).to_be_visible()


@pytest.mark.parametrize(("locale", "width"), [("cs-CZ", 390), ("en-GB", 1440)])
def test_cuisine_shortcuts_quick_save_reuse_and_enqueue(
    cuisine_site: Any, locale: str, width: int, tmp_path: Path
) -> None:
    executable = shutil.which("chromium")
    if not executable:
        pytest.skip("Chromium is required")
    origin, _ = cuisine_site
    lang = "cs" if locale.startswith("cs") else "en"
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
        writes: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "request",
            lambda request: (
                writes.append(urlparse(request.url).path) if request.method == "POST" else None
            ),
        )
        try:
            page.goto(origin)
            page.locator("#auth-register-tab").click()
            page.locator("#register-username").fill("cuisineuser")
            for field in ["register-password", "register-password-confirm"]:
                page.locator(f"#{field}").fill("a long cuisine fixture password")
            page.locator("#register-form button").click()
            playwright.expect(page.locator("#workspace")).to_be_visible()
            playwright.expect(page.locator("#register-form")).not_to_have_attribute(
                "aria-busy", "true"
            )
            playwright.expect(page.locator("#provider")).to_have_value("codex")
            playwright.expect(page.locator("#provider")).to_be_visible()
            playwright.expect(page.locator("#model")).to_be_visible()
            assert page.locator("#provider").evaluate("element => !element.closest('details')")
            playwright.expect(page.locator("#model")).to_have_value("gpt-6.1-sol")
            assert page.locator("#model option").evaluate_all(
                "options => options.map(option => option.value)"
            ) == list(TESTED_CODEX_MODELS)
            page.locator("#model").select_option("gpt-6-luna")
            playwright.expect(page.locator("#cuisine")).to_be_enabled()
            assert page.locator("#cuisine option").count() == 15
            assert page.request.get(f"{origin}/v1/me/presets").json()["items"] == []
            playwright.expect(page.locator("#update-preset")).to_be_disabled()
            playwright.expect(page.locator("#delete-preset")).to_be_disabled()

            page.locator("#budget").fill("81,5000")
            page.locator("#max-stores").fill("2")
            page.locator("#min-protein").evaluate(
                "element => element.closest('details').open = true"
            )
            page.locator("#min-protein").fill("40")
            page.locator("#max-kcal").fill("700")
            page.locator("#recipe-language").select_option("cs")
            navigate(page, "pantry")
            page.locator(".pantry-row select").first.select_option("rice")
            page.locator("[data-pantry-amount]").first.fill("450")
            page.locator("#condiment-salt").check()
            navigate(page, "recipes")

            def current_choices() -> Any:
                return page.evaluate("""() => Object.fromEntries(
                    ['budget', 'min-protein', 'max-kcal', 'max-stores', 'servings',
                     'meal-style', 'provider', 'model', 'recipe-language', 'recipe-count',
                     'recipe-retailers', 'cache-policy'].map(id =>
                         [id, document.getElementById(id).value])
                )""")

            before = current_choices()
            page.locator(".cuisine-shortcuts summary").click()
            for cuisine in CUISINE_IDS:
                button = page.locator(f'#cuisine-presets button[data-cuisine="{cuisine}"]')
                playwright.expect(button).to_have_text(CUISINE_LABELS[cuisine][lang])
                button.click()
                playwright.expect(button).to_have_attribute("aria-pressed", "true")
                playwright.expect(page.locator("#cuisine")).to_have_value(cuisine)
                assert current_choices() == before
                assert page.locator("[data-pantry-amount]").first.input_value() == "450"
                assert page.locator("#condiment-salt").is_checked()
            assert "/v1/recipes" not in writes and "/v1/me/presets" not in writes
            assert page.request.get(f"{origin}/v1/me/presets").json()["items"] == []
            page.locator('#cuisine-presets button[data-cuisine="greek"]').click()
            playwright.expect(page.locator("#quick-preset-name")).to_have_value(
                CUISINE_LABELS["greek"][lang]
            )

            page.locator("#provider").select_option("template")
            playwright.expect(page.locator("#cuisine")).to_be_disabled()
            playwright.expect(page.locator("#cuisine-hint")).to_be_visible()
            for button in page.locator("#cuisine-presets button").all():
                playwright.expect(button).to_be_disabled()
            page.locator("#provider").select_option("codex")
            playwright.expect(page.locator("#cuisine")).to_have_value("greek")
            assert current_choices() == before
            page.locator("#provider").select_option("ollama")
            assert page.locator("#model option").evaluate_all(
                "options => options.map(option => option.value)"
            ) == list(BENCHMARKED_LOCAL_MODELS)
            for model in BENCHMARKED_LOCAL_MODELS:
                page.locator("#model").select_option(model)
                playwright.expect(page.locator("#model")).to_have_value(model)
            weak_model = "codegemma:2b"
            page.locator("#provider").select_option("codex")
            playwright.expect(page.locator("#model")).to_have_value("gpt-6-luna")
            page.locator("#provider").select_option("ollama")
            playwright.expect(page.locator("#model")).to_have_value(weak_model)

            page.locator("#quick-preset-name").fill("")
            page.locator("#quick-save-preset").click()
            playwright.expect(page.locator("#quick-preset-name")).to_be_focused()
            playwright.expect(page.locator("#field-error-quick-preset-name")).to_be_visible()
            playwright.expect(page.locator("#quick-preset-status")).to_contain_text(
                "Zadejte název" if lang == "cs" else "Enter a preset name"
            )
            assert "/v1/me/presets" not in writes
            page.locator("#quick-preset-name").fill("My weeknight dinners")
            page.locator('#cuisine-presets button[data-cuisine="mexican"]').click()
            playwright.expect(page.locator("#quick-preset-name")).to_have_value(
                "My weeknight dinners"
            )
            page.locator('#cuisine-presets button[data-cuisine="greek"]').click()
            saved_name = "Greek"
            page.locator("#quick-preset-name").fill(saved_name)
            with page.expect_response(response_to("/v1/me/presets", "POST")) as saved:
                page.locator("#quick-preset-name").press("Enter")
            assert saved.value.status == 201
            preset = saved.value.json()
            assert preset["parameters"]["cuisine"] == "greek"
            assert preset["parameters"]["provider"] == "ollama"
            assert preset["parameters"]["model"] == weak_model
            assert preset["parameters"]["max_cost_per_serving_czk"] == "81.5000"
            assert (
                not {"pantry", "condiments", "use_first", "request_id"}
                & preset["parameters"].keys()
            )
            playwright.expect(page.locator("#preset-select")).to_have_value(preset["id"])
            playwright.expect(page.locator("#quick-preset-status")).to_contain_text(
                "Uloženo" if lang == "cs" else "Saved."
            )
            assert "/v1/recipes" not in writes
            assert page.request.get(f"{origin}/v1/jobs").json()["total"] == 0
            for ui_language in ["en", "cs", lang]:
                if page.locator("#ui-language").input_value() != ui_language:
                    with page.expect_response(response_to("/v1/me/settings", "PATCH")):
                        page.locator("#ui-language").select_option(ui_language)
                playwright.expect(page.locator("#quick-preset-name")).to_have_value(saved_name)
                playwright.expect(page.locator("#preset-name")).to_have_value(saved_name)
                playwright.expect(page.locator("#model")).to_have_value(weak_model)
                playwright.expect(
                    page.locator(f'#preset-select option[value="{preset["id"]}"]')
                ).to_have_text(saved_name)
            page.screenshot(path=str(tmp_path / "cuisine-presets.png"), full_page=True)

            page.locator("#budget").fill("21")
            page.locator("#provider").select_option("codex")
            page.locator('#cuisine-presets button[data-cuisine="chinese"]').click()
            page.locator("#apply-preset").click()
            playwright.expect(page.locator("#cuisine")).to_have_value("greek")
            playwright.expect(page.locator("#provider")).to_have_value("ollama")
            playwright.expect(page.locator("#model")).to_have_value(weak_model)
            playwright.expect(page.locator("#budget")).to_have_value("81.5000")
            assert page.locator("[data-pantry-amount]").first.input_value() == "450"
            assert page.locator("#condiment-salt").is_checked()

            def invalid_cuisine(route: Any) -> None:
                route.fulfill(
                    status=422,
                    json={
                        "detail": "Check cuisine",
                        "code": "validation-error",
                        "fields": [{"loc": ["body", "cuisine"]}],
                    },
                )

            page.route("**/v1/recipes", invalid_cuisine)
            page.locator("#generate").click()
            playwright.expect(page.locator("#cuisine")).to_be_focused()
            playwright.expect(page.locator("#cuisine")).to_have_attribute("aria-invalid", "true")
            page.unroute("**/v1/recipes", invalid_cuisine)
            with page.expect_response(response_to("/v1/recipes", "POST")) as queued:
                page.locator("#generate").click()
            assert queued.value.status == 202
            job_id = queued.value.json()["job_id"]
            assert queued.value.request.post_data_json["cuisine"] == "greek"
            assert queued.value.request.post_data_json["model"] == weak_model
            original = page.request.get(f"{origin}/v1/jobs/{job_id}/request").json()
            assert original["cuisine"] == "greek" and original["pantry"]["rice"] == "450g"
            assert page.request.get(f"{origin}/v1/jobs/{job_id}").json()["status"] == "queued"

            navigate(page, "account-settings")
            playwright.expect(page.locator("#preset-name")).to_have_value(saved_name)
            page.locator("#preset-name").fill("Updated dinners")
            with page.expect_response(
                response_to(f"/v1/me/presets/{preset['id']}", "PATCH")
            ) as updated:
                page.locator("#update-preset").click()
            assert updated.value.status == 200
            assert updated.value.request.post_data_json["expected_revision"] == preset["revision"]
            playwright.expect(page.locator("#preset-name")).to_have_value("Updated dinners")
            playwright.expect(page.locator("#delete-preset")).to_be_enabled()
            with page.expect_response(
                response_to(f"/v1/me/presets/{preset['id']}", "DELETE")
            ) as removed:
                page.locator("#delete-preset").click()
            assert removed.value.status == 204
            playwright.expect(page.locator("#update-preset")).to_be_disabled()
            playwright.expect(page.locator("#delete-preset")).to_be_disabled()
            assert page.request.get(f"{origin}/v1/jobs/{job_id}/request").json() == original
            assert page.request.get(f"{origin}/v1/me/presets").json()["items"] == []
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert not errors
        finally:
            context.close()
            browser.close()
