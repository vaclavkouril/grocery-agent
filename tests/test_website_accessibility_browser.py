"""Offline browser smoke coverage for bilingual accessibility and narrow reflow."""

import re
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

from tests.test_website_browser import registration_site  # noqa: F401

playwright = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.browser

PANELS = (
    ("recipes", "recipe-screen", "Recipes", "Recepty"),
    ("pantry", "pantry", "Pantry", "Spíž"),
    ("offers", "offers", "Grocery offers", "Nabídky potravin"),
    ("jobs", "jobs", "History", "Historie"),
    ("account-settings", "account-settings", "Account", "Účet"),
)


def assert_reflow(page: Any) -> None:
    dimensions = page.evaluate("""() => ({
        viewport: window.innerWidth,
        document: document.documentElement.scrollWidth,
        body: document.body.scrollWidth,
        bodyBounds: document.body.getBoundingClientRect().width
    })""")
    assert max(dimensions["document"], dimensions["body"]) <= dimensions["viewport"], dimensions
    assert dimensions["bodyBounds"] <= dimensions["viewport"], dimensions


def assert_visible_control_names_and_labels(page: Any) -> None:
    controls = page.locator(
        "input:not([type=hidden]):visible, select:visible, textarea:visible, "
        "button:visible, a[href]:visible, summary:visible, [role=button]:visible"
    )
    assert controls.count() > 0
    for control in controls.all():
        # Canonical native choice selects are visually clipped and deliberately
        # removed from the accessibility tree; their visible replacements are checked.
        if control.get_attribute("aria-hidden") == "true":
            continue
        playwright.expect(control).to_have_accessible_name(re.compile(r"\S"))
        if control.evaluate("element => ['INPUT', 'SELECT', 'TEXTAREA'].includes(element.tagName)"):
            assert control.evaluate(r"""element => {
                const visible = label => label && label.checkVisibility();
                return [...(element.labels ?? [])].some(visible)
                    || (element.getAttribute('aria-labelledby') ?? '').split(/\s+/)
                        .some(id => visible(document.getElementById(id)));
            }"""), (
                "Visible form control lacks a visible label: "
                f"{control.evaluate('e => e.outerHTML')}"
            )


def assert_reduced_motion(page: Any) -> None:
    assert page.evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches")
    assert page.evaluate("getComputedStyle(document.documentElement).scrollBehavior") == "auto"
    durations = page.locator("button:visible").evaluate_all("""elements => elements.flatMap(
      element => {
        const style = getComputedStyle(element);
        return [style.animationDuration, style.transitionDuration].flatMap(value =>
            value.split(',').map(duration => parseFloat(duration)));
    })""")
    assert durations and all(duration == 0 for duration in durations), durations


def assert_native_choices_skip_tab(page: Any, panel: Any) -> None:
    # Exercise actual sequential keyboard focus, not only the tabindex attribute.
    for search in panel.locator(".choice-list input[type=search]").all():
        if not search.is_visible():
            continue
        native = search.locator("xpath=../preceding-sibling::select[1]")
        playwright.expect(native).to_have_class(re.compile(r"\bnative-choices\b"))
        playwright.expect(native).to_have_attribute("tabindex", "-1")
        playwright.expect(native).to_have_attribute("aria-hidden", "true")
        assert not native.evaluate("element => element.disabled"), (
            "fixture must exercise enabled choices"
        )
        search.focus()
        page.keyboard.press("Shift+Tab")
        assert not page.locator("select.native-choices").evaluate_all(
            "elements => elements.includes(document.activeElement)"
        )
        page.keyboard.press("Tab")
        playwright.expect(search).to_be_focused()
        page.keyboard.press("Tab")
        assert not page.locator("select.native-choices").evaluate_all(
            "elements => elements.includes(document.activeElement)"
        )


@pytest.mark.parametrize("language", ["cs", "en"])
@pytest.mark.parametrize("width", [320, 768, 1440], ids=["mobile", "tablet", "desktop"])
def test_bilingual_accessibility_reflow_keyboard_and_reduced_motion(
    registration_site: Any,  # noqa: F811 -- imported pytest fixture
    language: str,
    width: int,
    tmp_path: Path,
) -> None:
    executable = shutil.which("chromium")
    if executable is None:
        pytest.skip("install Chromium to run fixture-backed browser acceptance")
    origin, _ = registration_site
    password = "a long accessibility fixture password"
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(executable_path=executable, headless=True)
        context = browser.new_context(
            viewport={"width": width, "height": 1000},
            locale=f"{language}-{'CZ' if language == 'cs' else 'GB'}",
            reduced_motion="reduce",
            service_workers="block",
        )
        blocked: list[str] = []

        def local_only(route: Any) -> None:
            if urlparse(route.request.url).netloc == urlparse(origin).netloc:
                route.continue_()
            else:
                blocked.append(route.request.url)
                route.abort()

        context.route("**/*", local_only)
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(origin)
            playwright.expect(page.locator("html")).to_have_attribute("lang", language)
            playwright.expect(page.locator("#auth-register-tab")).to_be_visible()
            assert_reflow(page)
            assert_visible_control_names_and_labels(page)
            assert_reduced_motion(page)
            page.screenshot(path=str(tmp_path / f"{language}-{width}-signin.png"), full_page=True)

            # Skip link and registration tabs are operated from the keyboard.
            page.keyboard.press("Tab")
            skip = page.get_by_role(
                "link",
                name=("Přejít na hlavní obsah" if language == "cs" else "Skip to main content"),
                exact=True,
            )
            playwright.expect(skip).to_be_focused()
            page.keyboard.press("Enter")
            playwright.expect(page.locator("#main-content")).to_be_focused()
            login_tab = page.get_by_role(
                "tab", name=("Přihlásit se" if language == "cs" else "Sign in"), exact=True
            )
            login_tab.focus()
            page.keyboard.press("ArrowRight")
            register_tab = page.get_by_role(
                "tab", name=("Vytvořit účet" if language == "cs" else "Create account"), exact=True
            )
            playwright.expect(register_tab).to_be_focused()
            page.keyboard.press("Enter")
            playwright.expect(page.locator("#register-form")).to_be_visible()
            assert_visible_control_names_and_labels(page)
            assert_reflow(page)
            page.locator("#register-username").fill(f"access_{language}_{width}")
            page.locator("#register-password").fill(password)
            page.locator("#register-password-confirm").fill(password)
            page.locator("#register-form").get_by_role(
                "button",
                name=("Vytvořit účet" if language == "cs" else "Create account"),
                exact=True,
            ).press("Enter")
            playwright.expect(page.locator("#workspace")).to_be_visible()
            playwright.expect(page.locator("#username")).to_have_text(f"access_{language}_{width}")
            playwright.expect(page.locator("#admin-link")).to_be_hidden()
            playwright.expect(page.locator("#admin")).to_be_hidden()
            playwright.expect(page.locator("#save-settings")).to_be_enabled()

            nav = page.get_by_role(
                "navigation",
                name=("Pracovní prostor" if language == "cs" else "Workspace"),
                exact=True,
            )
            assert nav.get_by_role("link").count() == 5
            for route, panel_id, english, czech in PANELS:
                link = nav.get_by_role(
                    "link", name=czech if language == "cs" else english, exact=True
                )
                link.focus()
                page.keyboard.press("Enter")
                playwright.expect(page).to_have_url(re.compile(rf"#{re.escape(route)}$"))
                playwright.expect(link).to_have_attribute("aria-current", "page")
                panel = page.locator(f"#{panel_id}")
                playwright.expect(panel).to_be_visible()
                for _, other_id, _, _ in PANELS:
                    if other_id != panel_id:
                        playwright.expect(page.locator(f"#{other_id}")).to_be_hidden()
                # Include every advanced form control rather than only closed summaries.
                for summary in panel.locator("details > summary").all():
                    if summary.is_visible() and not summary.evaluate(
                        "element => element.parentElement.open"
                    ):
                        summary.press("Space")
                assert_visible_control_names_and_labels(page)
                assert_native_choices_skip_tab(page, panel)
                assert_reflow(page)
                assert_reduced_motion(page)
                page.screenshot(
                    path=str(tmp_path / f"{language}-{width}-{route}.png"), full_page=True
                )
                if width == 1440 and route == "recipes":
                    # CSS zoom is deterministic in headless Chromium and exercises
                    # enlarged controls/content without changing the desktop viewport.
                    page.locator("html").evaluate("element => element.style.zoom = '2'")
                    try:
                        assert_reflow(page)
                        assert_visible_control_names_and_labels(page)
                        page.screenshot(
                            path=str(tmp_path / f"{language}-{width}-recipes-zoom-200.png"),
                            full_page=True,
                        )
                    finally:
                        page.locator("html").evaluate("element => element.style.zoom = '1'")
                    assert_reflow(page)

            # An ordinary user can reach History via Tab, with no admin navigation stop.
            nav.get_by_role(
                "link", name="Recepty" if language == "cs" else "Recipes", exact=True
            ).focus()
            for route, _, english, czech in PANELS[1:]:
                page.keyboard.press("Tab")
                link = nav.get_by_role(
                    "link", name=czech if language == "cs" else english, exact=True
                )
                playwright.expect(link).to_be_focused()
                if route == "jobs":
                    page.keyboard.press("Enter")
                    playwright.expect(page.locator("#jobs")).to_be_visible()
                    playwright.expect(link).to_have_attribute("aria-current", "page")
                    break
            nav.get_by_role(
                "link",
                name="Účet" if language == "cs" else "Account",
                exact=True,
            ).focus()
            page.keyboard.press("Tab")
            assert page.evaluate("document.activeElement.id !== 'admin-link'")
            assert page.evaluate("document.activeElement.closest('[hidden]') === null")
            assert_reflow(page)
            assert not errors, errors
            assert not blocked, f"Page attempted requests outside the fixture origin: {blocked}"
        finally:
            page.screenshot(
                path=str(tmp_path / f"{language}-{width}-last-state.png"), full_page=True
            )
            context.close()
            browser.close()
