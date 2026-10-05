"""Bilingual UI acceptance against the real account, recipe and catalogue API."""

import asyncio
import shutil
from concurrent.futures import ThreadPoolExecutor
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
ROOT = browser_fixtures.ROOT
registration_site = browser_fixtures.registration_site


def navigate(page: Any, screen: str) -> None:
    page.locator(f'.tabs a[href="#{screen}"]').click()
    playwright.expect(
        page.locator("#recipe-screen" if screen == "recipes" else f"#{screen}")
    ).to_be_visible()


@pytest.mark.parametrize(("locale", "width"), [("cs-CZ", 390), ("en-GB", 1440)])
def test_bilingual_account_pantry_presets_validation_offers_history(
    registration_site: Any, engine: Any, locale: str, width: int, tmp_path: Any
) -> None:
    executable = shutil.which("chromium")
    if not executable:
        pytest.skip("Chromium is required")
    origin, repo = registration_site
    cs = locale.startswith("cs")
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(executable_path=executable, headless=True)
        context = browser.new_context(
            locale=locale, viewport={"width": width, "height": 900}, accept_downloads=True
        )
        context.route(
            "**/*",
            lambda route: (
                route.continue_()
                if urlparse(route.request.url).hostname in {"localhost", "127.0.0.1"}
                else route.abort()
            ),
        )
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(origin)
            assert page.locator("html").get_attribute("lang") == ("cs" if cs else "en")
            page.locator("#auth-register-tab").click()
            page.locator("#register-username").fill("bilingual")
            for field in ["register-password", "register-password-confirm"]:
                page.locator(f"#{field}").fill("a long bilingual fixture password")
            page.locator("#register-form button").click()
            playwright.expect(page.locator("#workspace")).to_be_visible()
            playwright.expect(page.locator("#save-settings")).to_be_enabled()
            playwright.expect(page.locator("#save-pantry")).to_be_enabled()
            for screen in ["pantry", "offers", "jobs", "account-settings", "admin"]:
                playwright.expect(page.locator(f"#{screen}")).to_be_hidden()
            assert page.locator("#recipe-form > details").count() == 5
            assert page.evaluate("localStorage.length + sessionStorage.length") == 0

            # Structured backend 422 errors focus the exact control and never echo invalid input.
            page.locator("#min-protein").evaluate("el => el.closest('details').open = true")
            page.locator("#min-protein").fill("400")
            with page.expect_response(
                lambda response: (
                    urlparse(response.url).path == "/v1/recipes"
                    and response.request.method == "POST"
                )
            ) as invalid:
                page.locator("#generate").click()
            assert invalid.value.status == 422
            playwright.expect(page.locator("#min-protein")).to_be_focused()
            playwright.expect(page.locator("#min-protein")).to_have_attribute(
                "aria-invalid", "true"
            )
            playwright.expect(page.locator("#field-error-min-protein")).to_contain_text(
                "Bílkoviny" if cs else "Protein"
            )
            assert "400" not in page.locator("#field-error-min-protein").inner_text()
            page.locator("#min-protein").fill("")
            playwright.expect(page.locator("#field-error-min-protein")).to_have_count(0)

            # Checkbox focus remains on the same ingredient after Space and after filtering.
            page.locator("#exclusions").evaluate("el => el.closest('details').open = true")
            page.locator("#exclusions-search").fill("onion")
            checkbox = page.locator("#exclusions + .choice-list input[type=checkbox]").first
            checkbox.focus()
            page.keyboard.press("Space")
            playwright.expect(checkbox).to_be_focused()
            page.keyboard.press("Space")
            playwright.expect(checkbox).to_be_focused()
            assert page.locator("#exclusions").get_attribute("tabindex") == "-1"

            navigate(page, "pantry")
            page.locator("#pantry-rows select").select_option("rice")
            page.locator("#pantry-rows input").fill("1,000001kg")
            page.locator("#use-first").select_option(["rice"])
            with page.expect_response(
                lambda response: (
                    urlparse(response.url).path == "/v1/me/pantry"
                    and response.request.method == "PUT"
                )
            ) as saved:
                page.locator("#save-pantry").click()
            assert saved.value.json()["items"]["rice"] == {"grams": "1000.001", "use_first": True}
            assert saved.value.request.post_data_json["expected_revision"] == 0
            csrf = page.request.get(f"{origin}/v1/auth/session").json()["csrf_token"]
            headers = {"X-CSRF-Token": csrf}
            stock = page.request.get(f"{origin}/v1/me/pantry").json()
            assert page.request.put(
                f"{origin}/v1/me/pantry",
                headers=headers,
                data={
                    "expected_revision": stock["revision"],
                    "items": {"rice": {"grams": "600", "use_first": True}},
                },
            ).ok
            page.locator("#pantry-rows input").fill("500g")
            page.locator("#save-pantry").click()
            playwright.expect(page.locator("#pantry-status")).to_contain_text(
                "jiné relaci" if cs else "another session"
            )
            assert page.locator("#pantry-rows input").input_value() == "500g"
            playwright.expect(page.locator("#save-pantry")).to_be_disabled()
            page.locator("#reload-pantry").click()
            playwright.expect(page.locator("#pantry-rows input")).to_have_value("600g")

            navigate(page, "recipes")
            page.locator("#budget").fill("80,0000")
            page.locator("#recipe-language").select_option("cs")
            page.locator("#have-seasonings").evaluate("el => el.closest('details').open = true")
            page.locator("#have-seasonings").select_option("false")
            navigate(page, "account-settings")
            page.locator("#preset-name").fill("Sign out")
            with page.expect_response(
                lambda response: (
                    urlparse(response.url).path == "/v1/me/presets"
                    and response.request.method == "POST"
                )
            ) as created:
                page.locator("#create-preset").click()
            preset = created.value.json()
            assert not {"pantry", "use_first", "request_id"} & preset["parameters"].keys()
            assert preset["parameters"]["seasonings_available"] is False
            assert preset["parameters"]["max_cost_per_serving_czk"] == "80.0000"
            playwright.expect(
                page.locator(f'#preset-select option[value="{preset["id"]}"]')
            ).to_have_text("Sign out")
            navigate(page, "recipes")
            page.locator("#preset-select").select_option(preset["id"])
            assert page.request.patch(
                f"{origin}/v1/me/presets/{preset['id']}",
                headers=headers,
                data={"name": "Remote", "expected_revision": preset["revision"]},
            ).ok
            page.locator("#servings").fill("3")
            navigate(page, "account-settings")
            page.locator("#preset-name").fill("Local edited name")
            page.locator("#update-preset").click()
            playwright.expect(page.locator("#preset-status")).to_contain_text(
                "jiné relaci" if cs else "another session"
            )
            page.locator("#reload-presets").click()
            playwright.expect(
                page.locator(f'#preset-select option[value="{preset["id"]}"]')
            ).to_have_text("Remote")
            assert page.locator("#preset-name").input_value() == "Local edited name"
            assert page.locator("#servings").input_value() == "3"
            navigate(page, "recipes")

            # Interface changes persist only the explicit language and never regenerate recipes.
            with page.expect_response(
                lambda response: (
                    urlparse(response.url).path == "/v1/me/settings"
                    and response.request.method == "PATCH"
                )
            ):
                page.locator("#ui-language").select_option("en" if cs else "cs")
            assert page.locator("#recipe-language").input_value() == "cs"
            assert page.evaluate("Object.keys(localStorage)") == ["grocery.ui_language"]
            assert page.evaluate("sessionStorage.length") == 0
            with page.expect_response(
                lambda response: (
                    urlparse(response.url).path == "/v1/me/settings"
                    and response.request.method == "PATCH"
                )
            ):
                page.locator("#ui-language").select_option("cs" if cs else "en")

            with page.expect_response(
                lambda response: (
                    urlparse(response.url).path == "/v1/recipes"
                    and response.request.method == "POST"
                )
            ) as submitted:
                page.locator("#generate").click()
            assert submitted.value.status == 202
            request = submitted.value.request.post_data_json
            assert request["language"] == "cs" and request["seasonings_available"] is False
            job_id = submitted.value.json()["job_id"]
            playwright.expect(page.locator("#request-summary")).to_be_visible()
            settings = Settings(
                database_url=str(engine.url), meal_config=ROOT / "config/meals.toml", _env_file=None
            )
            backend = BackendSettings(providers=("template",))
            worker = Worker(
                repo, RecipeExecutor(settings, backend, lambda: NOW), settings, backend, lambda: NOW
            )
            with ThreadPoolExecutor(max_workers=1) as pool:
                assert pool.submit(lambda: asyncio.run(worker.once())).result(timeout=10)
            page.locator("#check-job").click()
            playwright.expect(page.locator(".recipe-card").first).to_be_visible()
            playwright.expect(page.locator(".recipe-card").first).to_contain_text(
                "Nákupní kontext" if cs else "Shopping context"
            )
            if cs:
                assert "," in page.locator(".metric").first.inner_text()
            stock_after = page.request.get(f"{origin}/v1/me/pantry").json()
            assert stock_after["items"]["rice"]["grams"] == "600"
            with page.expect_download() as download:
                page.locator("#download-html").click()
            assert download.value.suggested_filename.endswith(".html")

            navigate(page, "offers")
            page.locator("#offer-unit").select_option("kg")
            page.locator("#offer-retailer-choices").select_option(["billa", "tesco"])
            page.locator("#offer-category").fill("produce")
            page.locator("#offer-max-price").fill("42,0001")
            page.locator("#offer-from").check()
            page.locator("#offer-unavailable").check()
            with page.expect_response(
                lambda response: urlparse(response.url).path == "/v1/offers"
            ) as offers:
                page.locator("#offers-form button").click()
            assert offers.value.ok
            query = parse_qs(urlparse(offers.value.url).query)
            assert query["retailer"] == ["billa", "tesco"]
            assert query["max_unit_price"] == ["42.0001"]
            assert query["include_from"] == query["include_unavailable"] == ["true"]
            assert query["category"] == ["produce"]

            navigate(page, "jobs")
            page.locator("#job-list button").filter(
                has_text="Znovu" if cs else "Reuse"
            ).first.click()
            playwright.expect(page.locator("#recipe-screen")).to_be_visible()
            assert page.locator("#recipe-language").input_value() == "cs"
            assert page.locator("#servings").input_value() == "3"
            with page.expect_response(
                lambda response: (
                    urlparse(response.url).path == "/v1/recipes"
                    and response.request.method == "POST"
                )
            ) as reused:
                page.locator("#generate").click()
            assert reused.value.status == 202 and reused.value.json()["job_id"] != job_id
            assert "request_id" not in reused.value.request.post_data_json

            page.screenshot(path=str(tmp_path / f"recipes-{locale}.png"), full_page=True)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.reload()
            playwright.expect(page.locator("#workspace")).to_be_visible()
            navigate(page, "pantry")
            playwright.expect(page.locator("#pantry-rows input")).to_have_value("600g")
            page.locator("#signout").click()
            playwright.expect(page.locator("#signin-panel")).to_be_visible()
            assert page.locator("#pantry-rows").inner_text() == ""
            assert page.locator("#binding-list").inner_text() == ""
            assert page.evaluate("Object.keys(localStorage)") == ["grocery.ui_language"]
            assert not errors, errors
        finally:
            context.close()
            browser.close()


def test_unsupported_browser_locale_defaults_to_czech(registration_site: Any) -> None:
    executable = shutil.which("chromium")
    if not executable:
        pytest.skip("Chromium is required")
    origin, _ = registration_site
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(executable_path=executable, headless=True)
        page = browser.new_page(locale="de-DE")
        page.goto(origin)
        playwright.expect(page.locator("html")).to_have_attribute("lang", "cs")
        assert page.evaluate("localStorage.length + sessionStorage.length") == 0
        browser.close()
