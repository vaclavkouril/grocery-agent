"""Private per-meal nutrition exports using the real offline API and browser."""

import asyncio
import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from grocery_agent.backend.config import BackendSettings
from grocery_agent.backend.recipes import RecipeExecutor
from grocery_agent.backend.worker import Worker
from grocery_agent.config import Settings
from tests import test_website_browser as browser_fixtures
from tests.application_support import NOW

playwright = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.browser
registration_site = browser_fixtures.registration_site


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
def test_private_selected_meal_exports_download_copy_and_stale_guards(
    registration_site: Any, engine: Any, locale: str, width: int, tmp_path: Path
) -> None:
    executable = shutil.which("chromium")
    if not executable:
        pytest.skip("Chromium is required")
    origin, repo = registration_site
    lang = "cs" if locale.startswith("cs") else "en"
    settings = Settings(
        database_url=str(engine.url),
        meal_config=browser_fixtures.ROOT / "config/meals.toml",
        _env_file=None,
    )
    backend = BackendSettings(providers=("template",))
    worker = Worker(
        repo, RecipeExecutor(settings, backend, lambda: NOW), settings, backend, lambda: NOW
    )
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
        context.add_init_script(
            "Object.defineProperty(navigator, 'clipboard', {configurable: true, "
            "value: {writeText: async () => {throw Error('clipboard denied')}}});"
        )
        page = context.new_page()
        page.set_default_timeout(10000)
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(origin)
            page.locator("#auth-register-tab").click()
            page.locator("#register-username").fill("exportowner")
            for field in ["register-password", "register-password-confirm"]:
                page.locator(f"#{field}").fill("a long export fixture password")
            page.locator("#register-form button").click()
            playwright.expect(page.locator("#save-settings")).to_be_enabled()
            page.locator("#budget").fill("90")
            with page.expect_response(response_to("/v1/recipes", "POST")) as submitted:
                page.locator("#generate").click()
            job_id = submitted.value.json()["job_id"]
            with ThreadPoolExecutor(max_workers=1) as pool:
                assert pool.submit(lambda: asyncio.run(worker.once())).result(timeout=10)
            page.locator("#check-job").click()
            playwright.expect(page.locator(".recipe-card").nth(1)).to_be_attached()
            card = page.locator(".recipe-card").nth(1)
            card.locator("summary").first.click()
            export = card.locator(".recipe-export")
            export.locator("summary").click()
            playwright.expect(export.locator("[data-export-language]")).to_have_value(lang)
            playwright.expect(export.locator("p.hint").first).to_contain_text(
                "Hmotnost po uvaření" if lang == "cs" else "Cooked mass"
            )
            path = f"/v1/jobs/{job_id}/recipes/1/export"
            export_requests: list[Any] = []
            page.on(
                "request",
                lambda request: (
                    export_requests.append(request)
                    if urlparse(request.url).path.endswith("/export")
                    else None
                ),
            )
            for format_id in ["text", "ingredients", "schema-org"]:
                export.locator("[data-export-format]").select_option(format_id)
                expected = page.request.get(f"{origin}{path}?format={format_id}&language={lang}")
                assert expected.ok
                with page.expect_download() as downloading:
                    export.locator('[data-export-action="download"]').click()
                downloaded = downloading.value
                assert downloaded.suggested_filename == (
                    f"recipe-{job_id}-1-{lang}-{format_id}."
                    + ("json" if format_id == "schema-org" else "txt")
                )
                assert Path(downloaded.path()).read_text() == expected.text()
                playwright.expect(export.locator("[data-export-preview]")).to_have_value(
                    expected.text()
                )
                if format_id == "schema-org":
                    assert json.loads(expected.text())["@type"] == "Recipe"
            export.locator("[data-export-format]").select_option("ingredients")
            with page.expect_response(response_to(path)):
                export.locator('[data-export-action="copy"]').click()
            preview = export.locator("[data-export-preview]")
            playwright.expect(preview).to_be_focused()
            assert preview.evaluate("el => el.readOnly && el.selectionEnd === el.value.length")
            playwright.expect(export.locator('[role="status"]')).to_contain_text(
                "Schránka" if lang == "cs" else "Clipboard unavailable"
            )
            page.screenshot(path=str(tmp_path / "recipe-export.png"), full_page=True)
            export.screenshot(path=str(tmp_path / "recipe-export-controls.png"))
            page.evaluate(
                "navigator.clipboard.writeText = async text => {window.copiedExport = text}"
            )
            export.locator('[data-export-action="copy"]').click()
            playwright.expect(export.locator('[role="status"]')).to_contain_text(
                "zkopírován" if lang == "cs" else "Export copied"
            )
            assert page.evaluate("window.copiedExport") == preview.input_value()

            # A new pending request leaves the original meal's export provenance intact.
            with page.expect_response(response_to("/v1/recipes", "POST")) as newer:
                page.locator("#generate").click()
            assert newer.value.json()["job_id"] != job_id
            with page.expect_response(response_to(path)):
                export.locator('[data-export-action="copy"]').click()
            assert all(urlparse(request.url).path == path for request in export_requests)
            assert all(
                parse_qs(urlparse(request.url).query)["language"] == [lang]
                for request in export_requests
            )

            # Changing selection while a response is pending invalidates that response.
            pending: list[tuple[Any, Any]] = []
            held_count = 0

            def hold_export(route: Any) -> None:
                nonlocal held_count
                pending.append((route, route.fetch()))
                held_count += 1
                page.locator("html").evaluate(
                    "(el, count) => el.dataset.heldExport = String(count)", held_count
                )

            def copy_pending(control: Any) -> None:
                expected_count = held_count + 1
                control.locator('[data-export-action="copy"]').click()
                playwright.expect(page.locator("html")).to_have_attribute(
                    "data-held-export", str(expected_count)
                )
                playwright.expect(control.locator('[data-export-action="copy"]')).to_be_disabled()

            page.route("**/recipes/*/export?*", hold_export)
            copy_pending(export)
            playwright.expect(export.locator('[data-export-action="download"]')).to_be_enabled()
            assert len(pending) == 1
            export.locator("[data-export-language]").select_option("en" if lang == "cs" else "cs")
            route, response = pending.pop()
            route.fulfill(response=response)
            playwright.expect(export.locator('[data-export-action="copy"]')).to_be_enabled()
            playwright.expect(preview).to_be_hidden()
            assert preview.input_value() == ""
            copy_pending(export)
            export.locator("[data-export-format]").select_option("text")
            route, response = pending.pop()
            route.fulfill(response=response)
            playwright.expect(export.locator('[data-export-action="copy"]')).to_be_enabled()
            playwright.expect(preview).to_be_hidden()
            page.unroute("**/recipes/*/export?*", hold_export)

            navigate(page, "jobs")
            page.locator(f'#job-list li[data-job-id="{job_id}"] button').first.click()
            navigate(page, "recipes")
            playwright.expect(page.locator("#result")).to_have_attribute("data-job-id", job_id)
            card = page.locator(".recipe-card").nth(1)
            if not card.evaluate("el => el.open"):
                card.locator("summary").first.click()
            export = card.locator(".recipe-export")
            export.locator("summary").click()
            with page.expect_response(response_to(path)):
                export.locator('[data-export-action="copy"]').click()
            playwright.expect(export.locator('[role="status"]')).to_contain_text(
                "zkopírován" if lang == "cs" else "Export copied"
            )

            with page.expect_response(response_to("/v1/me/saved-recipes", "POST")):
                card.locator("[data-bookmark-key]").click()
            navigate(page, "bookmarks")
            page.locator("#bookmark-list li button").first.click()
            navigate(page, "recipes")
            playwright.expect(page.locator("#result")).to_have_attribute("data-job-id", job_id)
            card = page.locator(".recipe-card").nth(1)
            playwright.expect(card).to_have_attribute("open", "")
            export = card.locator(".recipe-export")
            export.locator("summary").click()
            with page.expect_response(response_to(path)):
                export.locator('[data-export-action="copy"]').click()
            playwright.expect(export.locator('[role="status"]')).to_contain_text(
                "zkopírován" if lang == "cs" else "Export copied"
            )

            # Locale rerender and session removal cannot revive an old private preview.
            page.route("**/recipes/*/export?*", hold_export)
            copy_pending(export)
            with page.expect_response(response_to("/v1/me/settings", "PATCH")):
                page.locator("#ui-language").select_option("en" if lang == "cs" else "cs")
            route, response = pending.pop()
            route.fulfill(response=response)
            playwright.expect(page.locator("[data-export-preview]").first).to_be_hidden()
            card = page.locator(".recipe-card").nth(1)
            export = card.locator(".recipe-export")
            export.locator("summary").click()
            copy_pending(export)
            page.locator("#signout").click()
            playwright.expect(page.locator("#signin-panel")).to_be_visible()
            route, response = pending.pop()
            route.fulfill(response=response)
            playwright.expect(page.locator(".recipe-export")).to_have_count(0)
            assert page.request.get(f"{origin}{path}?format=text&language={lang}").status == 401
            page.locator("#auth-register-tab").click()
            page.locator("#register-username").fill("exportother")
            for field in ["register-password", "register-password-confirm"]:
                page.locator(f"#{field}").fill("a different export fixture password")
            page.locator("#register-form button").click()
            playwright.expect(page.locator("#save-settings")).to_be_enabled()
            assert page.request.get(f"{origin}{path}?format=text&language={lang}").status == 404
            assert not errors
        finally:
            context.close()
            browser.close()
