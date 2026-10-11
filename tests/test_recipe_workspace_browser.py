"""Visible workspace controls against the real API, worker and private bookmark store."""

import asyncio
import copy
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


def store_chip(page: Any, field: str, store: str) -> Any:
    return page.locator(f'#{field}-choices + .choice-list input[data-option-value="{store}"]')


def response_to(path: str, method: str = "GET") -> Any:
    return lambda response: (
        urlparse(response.url).path == path and response.request.method == method
    )


@pytest.mark.parametrize(("locale", "width"), [("cs-CZ", 390), ("en-GB", 1440)])
def test_recipe_workspace_visible_inventory_stores_bookmarks_and_batches(
    registration_site: Any, engine: Any, locale: str, width: int, tmp_path: Any
) -> None:
    executable = shutil.which("chromium")
    if not executable:
        pytest.skip("Chromium is required")
    origin, repo = registration_site
    cs = locale.startswith("cs")
    settings = Settings(
        database_url=str(engine.url), meal_config=ROOT / "config/meals.toml", _env_file=None
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
        page = context.new_page()
        page.set_default_timeout(10000)
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(origin)
            page.locator("#auth-register-tab").click()
            page.locator("#register-username").fill("workspace")
            for field in ["register-password", "register-password-confirm"]:
                page.locator(f"#{field}").fill("a long workspace fixture password")
            page.locator("#register-form button").click()
            playwright.expect(page.locator("#workspace")).to_be_visible()
            playwright.expect(page.locator("#save-settings")).to_be_enabled()
            playwright.expect(page.locator("#recipe-count")).to_have_value("3")
            playwright.expect(page.locator("#have-seasonings")).to_be_hidden()

            navigate(page, "account-settings")
            store_chip(page, "account-retailers", "billa").check()
            store_chip(page, "account-retailers", "tesco").check()
            page.locator("#account-max-stores").fill("2")
            with page.expect_response(response_to("/v1/me/settings", "PATCH")) as saved_settings:
                page.locator("#save-settings").click()
            preference = saved_settings.value.request.post_data_json
            assert preference["retailer_ids"] == ["billa", "tesco"]
            assert preference["max_stores"] == 2
            playwright.expect(page.locator("#recipe-retailers")).to_have_value("billa, tesco")
            playwright.expect(page.locator("#offer-retailer")).to_have_value("billa, tesco")
            playwright.expect(page.locator("#max-stores")).to_have_value("2")

            navigate(page, "offers")
            with page.expect_response(response_to("/v1/offers")) as offers:
                page.locator("#offers-form button").click()
            assert parse_qs(urlparse(offers.value.url).query)["retailer"] == ["billa", "tesco"]
            store_chip(page, "offer-retailer", "billa").uncheck()
            with page.expect_response(response_to("/v1/offers")) as override:
                page.locator("#offers-form button").click()
            assert parse_qs(urlparse(override.value.url).query)["retailer"] == ["tesco"]
            assert page.request.get(f"{origin}/v1/me/settings").json()["retailer_ids"] == [
                "billa",
                "tesco",
            ]

            navigate(page, "pantry")
            row = page.locator(".pantry-row").first
            row.locator('input[type="search"]').fill("rice")
            row.locator("select").first.select_option("rice")
            row.locator("[data-pantry-amount]").fill("1,000001")
            row.locator("[data-pantry-unit]").select_option("kg")
            page.locator('#use-first + .choice-list input[data-option-value="rice"]').check()
            page.locator("#pantry-quick-add button").filter(
                has_text="olej" if cs else "oil"
            ).click()
            oil = page.locator(".pantry-row").last
            oil.locator("[data-pantry-unit]").select_option("available")
            playwright.expect(oil.locator("[data-pantry-amount]")).to_be_disabled()
            page.locator("#condiment-salt").check()
            page.locator("#condiment-black_pepper").check()
            with page.expect_response(response_to("/v1/me/pantry", "PUT")) as saved_stock:
                page.locator("#save-pantry").click()
            stock = saved_stock.value.json()
            page.screenshot(path=str(tmp_path / "workspace-pantry.png"), full_page=True)
            assert stock["items"]["rice"] == {"grams": "1000.001", "use_first": True}
            assert stock["items"]["oil"]["grams"] is None
            assert stock["condiments"] == ["salt", "black_pepper"]

            # Revisions preserve visible local edits until the user explicitly reloads.
            csrf = page.request.get(f"{origin}/v1/auth/session").json()["csrf_token"]
            headers = {"X-CSRF-Token": csrf}
            assert page.request.put(
                f"{origin}/v1/me/pantry",
                headers=headers,
                data={
                    "expected_revision": stock["revision"],
                    "items": stock["items"],
                    "condiments": [],
                },
            ).ok
            row.locator("[data-pantry-amount]").fill("0,5")
            page.locator("#save-pantry").click()
            playwright.expect(page.locator("#save-pantry")).to_be_disabled()
            playwright.expect(row.locator("[data-pantry-amount]")).to_have_value("0,5")
            page.locator("#reload-pantry").click()
            playwright.expect(page.locator("#save-pantry")).to_be_enabled()
            playwright.expect(page.locator("#condiment-salt")).not_to_be_checked()
            page.locator("#condiment-salt").check()
            with page.expect_response(response_to("/v1/me/pantry", "PUT")):
                page.locator("#save-pantry").click()

            navigate(page, "recipes")
            page.locator("#budget").fill("90")
            with page.expect_response(response_to("/v1/recipes", "POST")) as submitted:
                page.locator("#generate").click()
            assert submitted.value.status == 202
            request = submitted.value.request.post_data_json
            assert request["recipe_count"] == 3 and request["condiments"] == ["salt"]
            assert request["retailer_ids"] == ["billa", "tesco"] and request["max_stores"] == 2
            assert request["pantry"]["rice"] == "1000.001g"
            assert "seasonings_available" not in request
            job_id = submitted.value.json()["job_id"]
            with ThreadPoolExecutor(max_workers=1) as pool:
                assert pool.submit(lambda: asyncio.run(worker.once())).result(timeout=10)
            page.locator("#check-job").click()
            playwright.expect(page.locator(".recipe-card").first).to_be_visible()
            completed = page.request.get(f"{origin}/v1/jobs/{job_id}/result").json()
            assert len(completed["report"]["meals"]) >= 2
            assert page.locator(".recipe-card").count() == len(completed["report"]["meals"])
            first_title = completed["report"]["meals"][0]["title"]
            for index, meal in enumerate(completed["report"]["meals"]):
                expected_links = [
                    line["price"]["offer"]["source_url"]
                    for line in meal["lines"]
                    if float(line["purchased_grams"]) > 0
                    and line["price"].get("offer")
                    and line["price"]["offer"]["source_url"].startswith(("http://", "https://"))
                ]
                links = page.locator(".recipe-card").nth(index).locator("a[href]")
                assert (
                    links.evaluate_all("els => els.map(el => el.getAttribute('href'))")
                    == expected_links
                )
                assert all(
                    link["target"] == "_blank" and "noopener" in link["rel"]
                    for link in links.evaluate_all(
                        "els => els.map(el => ({target: el.target, rel: el.rel}))"
                    )
                )

            page.locator(".recipe-card").nth(1).locator("summary").first.click()
            save_button = page.locator(".recipe-card").first.locator("[data-bookmark-key]")
            save_button.focus()
            with page.expect_response(response_to("/v1/me/saved-recipes", "POST")) as bookmarked:
                save_button.press("Enter")
            bookmark = bookmarked.value.json()
            assert bookmark["job_id"] == job_id and bookmark["meal_index"] == 0
            playwright.expect(save_button).to_have_attribute("aria-disabled", "true")
            playwright.expect(save_button).to_be_focused()
            playwright.expect(page.locator(".recipe-card").nth(1)).to_have_attribute("open", "")
            playwright.expect(page.locator(".recipe-card").first).to_have_attribute("open", "")
            assert (
                page.request.post(
                    f"{origin}/v1/me/saved-recipes",
                    headers=headers,
                    data={"job_id": job_id, "meal_index": 0},
                ).json()["id"]
                == bookmark["id"]
            )

            # A pending new request keeps the old result, its links and its provenance.
            page.locator("#recipe-count").fill("1")
            page.locator("#recipe-retailers").evaluate(
                "element => element.closest('details').open = true"
            )
            store_chip(page, "recipe-retailers", "billa").uncheck()
            store_chip(page, "recipe-retailers", "tesco").uncheck()
            with page.expect_response(response_to("/v1/recipes", "POST")) as second:
                page.locator("#generate").click()
            assert second.value.json()["job_id"] != job_id
            assert second.value.request.post_data_json["retailer_ids"] == []
            assert page.request.get(f"{origin}/v1/me/settings").json()["retailer_ids"] == [
                "billa",
                "tesco",
            ]
            playwright.expect(page.locator("#result")).to_contain_text(first_title)
            playwright.expect(page.locator("#result")).to_contain_text(job_id)
            playwright.expect(page.locator("#downloads")).to_be_visible()
            if page.locator(".recipe-card").nth(1).get_attribute("open") is None:
                page.locator(".recipe-card").nth(1).locator("summary").first.click()
            with page.expect_response(
                response_to("/v1/me/saved-recipes", "POST")
            ) as second_bookmark:
                page.locator(".recipe-card").nth(1).locator("[data-bookmark-key]").click()
            assert second_bookmark.value.request.post_data_json == {
                "job_id": job_id,
                "meal_index": 1,
            }

            navigate(page, "bookmarks")
            playwright.expect(page.locator("#bookmark-list li")).to_have_count(2)
            page.screenshot(path=str(tmp_path / "workspace-bookmarks.png"), full_page=True)
            saved_row = page.locator("#bookmark-list li").filter(has_text=first_title)
            saved_row.get_by_role("button", name="Otevřít" if cs else "Open", exact=True).click()
            playwright.expect(page.locator(".recipe-card").first).to_have_attribute("open", "")
            playwright.expect(page.locator("#job-reference")).to_contain_text(job_id)
            navigate(page, "bookmarks")
            saved_row.get_by_role(
                "button", name="Znovu použít předvolby" if cs else "Reuse preferences", exact=True
            ).click()
            playwright.expect(page.locator("#recipe-count")).to_have_value("3")
            navigate(page, "bookmarks")
            saved_row.get_by_role(
                "button", name="Odstranit" if cs else "Remove", exact=True
            ).click()
            playwright.expect(page.locator("#bookmark-list li")).to_have_count(1)
            assert page.request.get(f"{origin}/v1/jobs/{job_id}").ok
            assert page.request.get(f"{origin}/v1/me/pantry").json()["items"] == stock["items"]

            foreign = runtime.request.new_context(base_url=origin)
            try:
                token = foreign.post(
                    "/v1/auth/register",
                    data={
                        "username": "otherworkspace",
                        "password": "a long other fixture password",
                        "session_mode": "bearer",
                    },
                ).json()["token"]
                private = {"Authorization": f"Bearer {token}"}
                saved_id = second_bookmark.value.json()["id"]
                assert (
                    foreign.get(f"/v1/me/saved-recipes/{saved_id}", headers=private).status == 404
                )
                assert (
                    foreign.post(
                        "/v1/me/saved-recipes",
                        headers=private,
                        data={"job_id": job_id, "meal_index": 0},
                    ).status
                    == 404
                )
            finally:
                foreign.dispose()

            # Render the frozen batch failure shape through a history result fetch.
            partial = copy.deepcopy(completed)
            partial["batch"] = {
                "requested_count": 3,
                "generated_count": 2,
                "failures": [{"index": 2, "code": "provider-invalid"}],
            }
            page.route(f"**/v1/jobs/{job_id}/result", lambda route: route.fulfill(json=partial))
            navigate(page, "jobs")
            page.locator(f'#job-list li[data-job-id="{job_id}"]').get_by_role(
                "button", name="Otevřít" if cs else "Open", exact=True
            ).click()
            playwright.expect(page.locator("#result .warning").first).to_contain_text(
                "Některé recepty" if cs else "Some recipes"
            )
            page.locator("#ui-language").select_option("en" if cs else "cs")
            playwright.expect(page.locator("#result .warning").first).to_contain_text(
                "Some recipes" if cs else "Některé recepty"
            )
            assert page.evaluate(
                "Object.keys(localStorage).every(key => key === 'grocery.ui_language')"
            )
            assert page.evaluate("sessionStorage.length") == 0
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.locator("#signout").click()
            playwright.expect(page.locator("#workspace")).to_be_hidden()
            assert page.locator("#bookmark-list").inner_text() == ""
            assert page.locator("#result").inner_text() == ""
            assert not errors
        finally:
            context.close()
            browser.close()
