"""Run locally with python -m grocery_agent.stores.makro.login."""

import importlib
import os
import shutil
import tempfile
from pathlib import Path

from grocery_agent.models.common import utc_now
from grocery_agent.stores.makro.config import MakroSettings
from grocery_agent.stores.makro.parser import category_url, parse_cards, snapshot


def save_login(settings: MakroSettings | None = None) -> Path:
    settings = settings or MakroSettings()
    try:
        sync_playwright = importlib.import_module("playwright.sync_api").sync_playwright
    except ImportError as exc:
        raise ValueError("Install the browser extra: pip install -e '.[browser]'") from exc
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=False,
            executable_path=(
                str(settings.browser_executable)
                if settings.browser_executable
                else shutil.which("chromium")
            ),
        )
        try:
            context = browser.new_context(locale="cs-CZ")
            page = context.new_page()
            page.goto(
                "https://sortiment.makro.cz/shop",
                wait_until="domcontentloaded",
                timeout=settings.navigation_timeout_ms,
            )
            input("Sign in in the browser, select your Makro branch, then press Enter here: ")
            url = category_url(settings.category_paths[0])
            page.goto(url, wait_until="domcontentloaded", timeout=settings.navigation_timeout_ms)
            page.locator(".sd-articlecard .price-display").first.wait_for(
                timeout=settings.navigation_timeout_ms
            )
            content = snapshot(page.content().encode())
            items = parse_cards(
                content,
                url,
                utc_now(),
                settings.account_scope,
                settings.category_paths[0],
                settings.store_name,
            )
            if not any(item.candidate for item in items):
                raise ValueError("Login has no supported VAT-inclusive prices; session not saved")
            destination = settings.session_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Create with private permissions before Playwright writes secrets.
            descriptor, temporary_name = tempfile.mkstemp(dir=destination.parent, suffix=".json")
            os.close(descriptor)
            temporary = Path(temporary_name)
            try:
                context.storage_state(path=str(temporary), indexed_db=True)
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
            return destination
        finally:
            browser.close()


def main() -> None:
    try:
        path = save_login()
    except (ValueError, EOFError) as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Makro login saved to {path}. Run grocery-agent scrape makro.")


if __name__ == "__main__":
    main()
