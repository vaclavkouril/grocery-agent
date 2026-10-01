import asyncio
import importlib
import json
import re
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass
from urllib.parse import unquote

from bs4 import BeautifulSoup

from grocery_agent.models.common import utc_now
from grocery_agent.models.offer import Offer
from grocery_agent.stores.base import AcquisitionItem, AdapterContext, StoreAdapter
from grocery_agent.stores.makro.config import MakroSettings
from grocery_agent.stores.makro.parser import (
    BASE_URL,
    PRODUCT_PATH,
    category_url,
    coverage,
    identity,
    parse_cards,
    snapshot,
)
from grocery_agent.stores.robots import RobotsPolicy


@dataclass(frozen=True)
class CatalogDocument:
    content: bytes
    url: str
    category: str
    # Bundle changes are additional offers, not additional category results.
    counts_towards_coverage: bool = True


class MakroAdapter(StoreAdapter):
    def __init__(self, settings: MakroSettings | None = None) -> None:
        self.settings = settings or MakroSettings()

    @property
    def store_id(self) -> str:
        return "makro"

    def validate_offer(self, offer: Offer) -> None:
        super().validate_offer(offer)
        match = re.fullmatch(PRODUCT_PATH, unquote(offer.source_url.path or ""))
        terms = offer.purchase_terms
        if (
            offer.source_url.host != "sortiment.makro.cz"
            or not match
            or offer.product.sku != ":".join(match.groups())
            or not offer.scope.startswith("makro:in-store:branch:")
            or not offer.scope.endswith(f":account:{self.settings.account_scope}")
            or offer.offer_key != "standard"
            or terms is None
            or not terms.vat_included
            or not terms.membership_required
        ):
            raise ValueError("candidate does not belong to the customer-scoped Makro catalog")

    async def _documents(self, context: AdapterContext) -> AsyncIterator[CatalogDocument]:
        if not self.settings.session_path.is_file():
            raise ValueError(
                "Makro requires a saved login session. Run: "
                "python -m grocery_agent.stores.makro.login"
            )
        try:
            async_playwright = importlib.import_module("playwright.async_api").async_playwright
        except ImportError as exc:
            raise ValueError("Makro requires: pip install -e '.[browser]'") from exc
        robots = await context.http.get(f"{BASE_URL}/robots.txt", follow_redirects=False)
        robots.raise_for_status()
        policy = RobotsPolicy.parse(robots.text, "grocery-agent")
        delay = max(policy.delay, self.settings.request_delay_seconds)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=self.settings.browser_headless,
                executable_path=(
                    str(self.settings.browser_executable)
                    if self.settings.browser_executable
                    else shutil.which("chromium")
                ),
            )
            try:
                session = await browser.new_context(
                    locale="cs-CZ", storage_state=str(self.settings.session_path)
                )
                page = await session.new_page()
                page.set_default_timeout(self.settings.navigation_timeout_ms)
                for category in self.settings.category_paths:
                    previous_count = 0
                    url = category_url(category)
                    if not policy.permits(url):
                        raise ValueError("robots.txt disallows the configured Makro category")
                    await asyncio.sleep(delay)
                    response = await page.goto(url, wait_until="domcontentloaded")
                    if response is None or response.status >= 400:
                        raise ValueError("Makro category navigation failed")
                    if page.url != url:
                        raise ValueError("Makro session expired or category redirected")
                    await page.locator(".sd-articlecard").first.wait_for()
                    await page.wait_for_function(
                        """() => Array.from(document.querySelectorAll('.sd-articlecard')).every(
                        c => c.querySelector('.price-display') ||
                        c.innerText.includes('Pro zobrazení cen se prosím přihlaste'))"""
                    )
                    for _ in range(self.settings.max_pages):
                        content = snapshot((await page.content()).encode())
                        yield CatalogDocument(content, url, category)
                        count, total = coverage(content)
                        # Visit every selectable bundle, including smaller packs. Prices
                        # in the dropdown may be net: capture the full card after selection.
                        cards = page.locator(".sd-articlecard")
                        for index in range(previous_count, await cards.count()):
                            card = cards.nth(index)
                            toggle = card.locator(".bundle.dropdown")
                            if await toggle.count() == 0:
                                continue
                            await toggle.click()
                            options = card.locator(
                                ".dropdown-menu [role='menuitem'], .dropdown-menu .dropdown-item"
                            )
                            labels = await options.locator(".packaging-type").all_text_contents()
                            if not labels or len(labels) != len(set(labels)):
                                raise ValueError("unable to enumerate distinct Makro pack options")
                            await toggle.click()
                            for label in labels:
                                selected = await card.locator(".bundle.packaging-type").inner_text()
                                if selected.strip() == label.strip():
                                    continue
                                await asyncio.sleep(delay)
                                old_href = await card.locator("a.title").get_attribute("href")
                                await toggle.click()
                                await (
                                    card.locator(".dropdown-menu")
                                    .get_by_text(label, exact=True)
                                    .click()
                                )
                                await page.wait_for_function(
                                    """([index, old]) => {
                                    const cards = document.querySelectorAll('.sd-articlecard');
                                    const card = cards[index];
                                    return card?.querySelector('a.title')
                                    ?.getAttribute('href') !== old;
                                    }""",
                                    arg=[index, old_href],
                                )
                                # Preserve the branch and coverage, but only this changed card.
                                partial = BeautifulSoup(await page.content(), "lxml")
                                all_cards = partial.select(".sd-articlecard")
                                for other_index, node in enumerate(all_cards):
                                    if other_index != index:
                                        node.decompose()
                                yield CatalogDocument(
                                    snapshot(str(partial).encode()), url, category, False
                                )
                        previous_count = count
                        if count == total:
                            break
                        more = page.get_by_text(re.compile(r"^Zobrazit \d+ další výsledky$"))
                        if await more.count() != 1:
                            raise ValueError("incomplete Makro category: missing load-more control")
                        await asyncio.sleep(delay)
                        await more.click()
                        await page.wait_for_function(
                            "count => document.querySelectorAll('.sd-articlecard').length > count",
                            arg=count,
                        )
                    else:
                        raise ValueError("max_pages reached before complete Makro coverage")
            finally:
                await browser.close()

    async def fetch_offers(self, context: AdapterContext) -> AsyncIterator[AcquisitionItem]:
        seen: dict[str, str] = {}
        categories: dict[str, tuple[int, int]] = {}
        category_variants: dict[str, set[str]] = {}
        had_error = False
        offer_scope: str | None = None
        async for document in self._documents(context):
            # Enforce evidence sanitation even for an injected transport.
            content = snapshot(document.content)
            if document.counts_towards_coverage:
                count, total = coverage(content)
                cards = BeautifulSoup(content, "lxml").select(".sd-articlecard")
                identities = {identity(card)[0].rsplit(":", 1)[0] for card in cards}
                if len(identities) != count or len(cards) != count:
                    raise ValueError("Makro result count disagrees with unique product variants")
                previous = categories.get(document.category)
                if previous and (previous[1] != total or previous[0] >= count):
                    raise ValueError("Makro pagination repeated or changed its total")
                if not category_variants.get(document.category, set()) <= identities:
                    raise ValueError("Makro pagination dropped previously shown products")
                category_variants[document.category] = identities
                categories[document.category] = count, total
            items = parse_cards(
                content,
                document.url,
                utc_now(),
                self.settings.account_scope,
                document.category,
                self.settings.store_name,
            )
            for item in items:
                if item.candidate is None:
                    had_error = True
                    yield item
                    continue
                candidate = item.candidate
                if offer_scope is not None and offer_scope != candidate["scope"]:
                    raise ValueError("Makro branch changed during acquisition")
                offer_scope = candidate["scope"]
                key = candidate["product"]["sku"]
                comparable = {
                    **candidate,
                    "product": {k: v for k, v in candidate["product"].items() if k != "category"},
                }
                fingerprint = json.dumps(comparable, sort_keys=True, default=str)
                if key in seen:
                    if seen[key] != fingerprint:
                        raise ValueError("Makro repeated a pack with different prices/terms")
                    continue
                seen[key] = fingerprint
                yield item
            if any(item.error and "sign in" in item.error for item in items):
                raise ValueError(
                    "Makro session expired or account cannot view prices; log in again"
                )
        if had_error or not seen:
            raise ValueError("Makro catalog contains unverified prices or purchase terms")
        if set(categories) != set(self.settings.category_paths) or any(
            c != t for c, t in categories.values()
        ):
            raise ValueError("incomplete Makro category coverage")
