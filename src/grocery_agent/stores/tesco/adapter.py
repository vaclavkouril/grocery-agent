import asyncio
import importlib
import json
import logging
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from email.utils import parsedate_to_datetime

import httpx

from grocery_agent.models.common import utc_now
from grocery_agent.models.offer import Offer
from grocery_agent.stores.base import AcquisitionItem, AdapterContext, SourceEvidence, StoreAdapter
from grocery_agent.stores.robots import RobotsPolicy
from grocery_agent.stores.tesco.config import TescoSettings
from grocery_agent.stores.tesco.parser import (
    BASE_URL,
    SCOPE,
    category_url,
    discover_categories,
    page_number,
    parse_page,
    public_snapshot,
)

FetchPage = Callable[[str], Awaitable[httpx.Response]]
logger = logging.getLogger(__name__)


class TescoAdapter(StoreAdapter):
    def __init__(self, settings: TescoSettings | None = None) -> None:
        self.settings = settings or TescoSettings()

    @property
    def store_id(self) -> str:
        return "tesco"

    def validate_offer(self, offer: Offer) -> None:
        super().validate_offer(offer)
        if (
            not offer.product.sku.isascii()
            or not offer.product.sku.isdigit()
            or offer.source_url.host != "nakup.itesco.cz"
            or offer.source_url.path != f"/shop/cs-CZ/products/{offer.product.sku}"
            or offer.scope != SCOPE
            or offer.offer_key not in {"standard", "loyalty"}
        ):
            raise ValueError(
                "candidate identity does not belong to the public Tesco online catalog"
            )

    @asynccontextmanager
    async def _session(self, context: AdapterContext) -> AsyncIterator[FetchPage]:
        try:
            async_playwright = importlib.import_module("playwright.async_api").async_playwright
        except ImportError as exc:
            raise ValueError(
                "Tesco requires the browser extra: pip install -e '.[browser]'"
            ) from exc
        executable = self.settings.browser_executable
        system_browser = shutil.which("chromium")
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=self.settings.browser_headless,
                executable_path=str(executable) if executable else system_browser,
            )
            try:
                session = await browser.new_context(locale="cs-CZ")
                page = await session.new_page()

                async def fetch(url: str) -> httpx.Response:
                    response = await page.goto(
                        url,
                        wait_until="domcontentloaded",
                        timeout=self.settings.navigation_timeout_ms,
                    )
                    if response is None:
                        raise ValueError("browser navigation returned no document response")
                    if page.url != url:
                        raise ValueError(
                            "Tesco navigation redirected outside the requested catalog"
                        )
                    return httpx.Response(
                        response.status,
                        content=await response.body(),
                        headers={
                            "Retry-After": await response.header_value("retry-after") or "",
                        },
                        request=httpx.Request("GET", url),
                    )

                yield fetch
            finally:
                await browser.close()

    async def _get(self, fetch: FetchPage, url: str, delay: float) -> httpx.Response:
        for attempt in range(self.settings.attempts):
            await asyncio.sleep(delay)
            response = await fetch(url)
            if response.status_code not in {429, 502, 503, 504}:
                break
            if attempt + 1 == self.settings.attempts:
                break
            retry_after = response.headers.get("Retry-After", "")
            wait = float(2**attempt)
            if retry_after.isdigit():
                wait = float(retry_after)
            elif retry_after:
                try:
                    wait = max(0, (parsedate_to_datetime(retry_after) - utc_now()).total_seconds())
                except (ValueError, TypeError, OverflowError):
                    pass
            if wait > 60:
                break
            await asyncio.sleep(wait)
        response.raise_for_status()
        return response

    async def fetch_offers(self, context: AdapterContext) -> AsyncIterator[AcquisitionItem]:
        async with self._session(context) as fetch:
            delay = self.settings.request_delay_seconds
            robots = await self._get(fetch, f"{BASE_URL}/robots.txt", delay)
            # A genuine browser uses its normal UA. Wildcard robots directives apply.
            policy = RobotsPolicy.parse(robots.text, "grocery-agent")
            delay = max(delay, policy.delay)

            async def get(url: str) -> httpx.Response:
                if not policy.permits(url):
                    raise ValueError(f"robots.txt disallows catalog request: {url}")
                return await self._get(fetch, url, delay)

            initial_slug = (
                self.settings.category_slugs[0]
                if self.settings.category_slugs
                else "ovoce-a-zelenina"
            )
            initial_url = category_url(initial_slug)
            first = await get(initial_url)
            first_content = public_snapshot(first.content)
            available = discover_categories(first_content)
            selected = self.settings.category_slugs or tuple(available)
            if any(slug not in available for slug in selected):
                raise ValueError("configured department missing from the public catalog navigation")
            seen: dict[tuple[str, str], str] = {}
            for slug in selected:
                listing_url = category_url(slug)
                url: str | None = listing_url
                category_seen: set[str] = set()
                expected_total: int | None = None
                expected_start = 1
                fields = {
                    "source_id": self.source_id,
                    "run_id": context.run_id,
                    "category_slug": slug,
                    "scope": SCOPE,
                }
                logger.info("category_started", extra={"fields": fields})
                for index in range(self.settings.max_pages):
                    if url is None:
                        break
                    if page_number(url, listing_url) != index + 1:
                        raise ValueError("pagination cycle or skipped page")
                    response = first if url == initial_url else await get(url)
                    fetched_at = utc_now()
                    content = (
                        first_content if url == initial_url else public_snapshot(response.content)
                    )
                    try:
                        page = parse_page(content, url, fetched_at, available[slug])
                        if expected_total is not None and expected_total != page.total:
                            raise ValueError("category total changed between pages")
                        if page.start != expected_start or category_seen.intersection(page.skus):
                            raise ValueError("pagination repeated, skipped or changed products")
                    except ValueError as exc:
                        # Do not retain original session/tracking scripts on a failed parse.
                        evidence = SourceEvidence(
                            content,
                            url,
                            "text/html",
                            fetched_at,
                            "document",
                            {"source_id": self.source_id},
                        )
                        yield AcquisitionItem(evidence=evidence, error=f"catalog failure: {exc}")
                        raise
                    expected_total, expected_start = page.total, page.end + 1
                    category_seen.update(page.skus)
                    for item in page.items:
                        if item.candidate is None:
                            yield item
                            continue
                        candidate = item.candidate
                        key = (candidate["product"]["sku"], candidate["offer_key"])
                        comparable = {
                            **candidate,
                            "product": {
                                k: v for k, v in candidate["product"].items() if k != "category"
                            },
                        }
                        fingerprint = json.dumps(comparable, sort_keys=True, default=str)
                        if key in seen:
                            if seen[key] != fingerprint:
                                yield AcquisitionItem(
                                    evidence=item.evidence,
                                    error="conflicting repeated product offer",
                                )
                            continue
                        seen[key] = fingerprint
                        yield item
                    url = page.next_url
                    logger.info(
                        "category_page_finished",
                        extra={
                            "fields": {
                                **fields,
                                "page": index + 1,
                                "products_seen": len(category_seen),
                                "total": expected_total,
                            }
                        },
                    )
                if url is not None or len(category_seen) != expected_total:
                    raise ValueError("max_pages reached or incomplete department coverage")
                logger.info(
                    "category_finished",
                    extra={
                        "fields": {
                            **fields,
                            "products": len(category_seen),
                        }
                    },
                )
