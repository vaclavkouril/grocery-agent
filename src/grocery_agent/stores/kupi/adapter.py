import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlsplit

import httpx

from grocery_agent.models.common import utc_now
from grocery_agent.models.offer import Offer
from grocery_agent.stores.base import (
    AcquisitionAdapter,
    AcquisitionItem,
    AdapterContext,
    SourceEvidence,
)
from grocery_agent.stores.kupi.config import KupiSettings
from grocery_agent.stores.kupi.parser import parse_page
from grocery_agent.stores.kupi.robots import RobotsPolicy

logger = logging.getLogger(__name__)


@dataclass
class _CoverageState:
    locality: str | None = None
    seen: dict[str, str] = field(default_factory=dict)


class KupiAdapter(AcquisitionAdapter):
    def __init__(self, settings: KupiSettings | None = None) -> None:
        self.settings = settings or KupiSettings()

    @property
    def source_id(self) -> str:
        return "kupi"

    def validate_offer(self, offer: Offer) -> None:
        if (
            not offer.product.sku.startswith("kupi:")
            or not offer.offer_key.startswith("kupi:")
            or not offer.scope.startswith("kupi:locality:")
            or offer.source_url.host != "www.kupi.cz"
        ):
            raise ValueError("candidate identity does not belong to Kupi")

    def _page_number(self, url: str, listing_url: str) -> int:
        parsed = urlsplit(url)
        initial = urlsplit(listing_url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        if (
            parsed.scheme != initial.scheme
            or parsed.netloc != initial.netloc
            or parsed.path != initial.path
            or parsed.fragment
            or set(query) - {"page"}
            or ("page" in query and (len(query["page"]) != 1 or not query["page"][0].isdigit()))
        ):
            raise ValueError("pagination escaped the configured public category")
        number = int(query.get("page", ["1"])[0])
        if number < 1:
            raise ValueError("invalid page number")
        return number

    async def _get(self, context: AdapterContext, url: str, delay: float) -> httpx.Response:
        for attempt in range(self.settings.attempts):
            await asyncio.sleep(delay)
            try:
                response = await context.http.get(url, follow_redirects=False)
            except httpx.TransportError:
                if attempt + 1 == self.settings.attempts:
                    raise
                await asyncio.sleep(2**attempt)
                continue
            if response.status_code not in {429, 502, 503, 504}:
                return response
            if attempt + 1 == self.settings.attempts:
                return response
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
                return response  # Fail visibly; do not retry before the server permits it.
            await asyncio.sleep(wait)
        raise AssertionError("unreachable retry loop")

    async def fetch_offers(self, context: AdapterContext) -> AsyncIterator[AcquisitionItem]:
        robots_url = "https://www.kupi.cz/robots.txt"
        delay = self.settings.request_delay_seconds
        robots = await self._get(context, robots_url, delay)
        robots.raise_for_status()
        policy = RobotsPolicy.parse(robots.text, context.http.headers.get("User-Agent", ""))
        delay = max(delay, policy.delay)
        state = _CoverageState()
        failures = []
        for listing_url in self.settings.listing_urls:
            try:
                async for item in self._fetch_category(context, listing_url, policy, delay, state):
                    yield item
            except (httpx.HTTPError, ValueError) as exc:
                # Access/rate denial applies to the source; stop before making further requests.
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {
                    403,
                    429,
                }:
                    raise
                failures.append(f"{listing_url}: {type(exc).__name__}: {exc}")
        if failures:
            raise ValueError("incomplete category coverage: " + "; ".join(failures))

    async def _fetch_category(
        self,
        context: AdapterContext,
        listing_url: str,
        policy: RobotsPolicy,
        delay: float,
        state: _CoverageState,
    ) -> AsyncIterator[AcquisitionItem]:
        fields = {
            "source_id": self.source_id,
            "run_id": context.run_id,
            "category_url": listing_url,
            "category_slug": urlsplit(listing_url).path.rsplit("/", 1)[-1],
        }
        logger.info("category_started", extra={"fields": fields})
        counters = {"pages": 0, "emitted": 0, "duplicates": 0, "parser_errors": 0}
        status = "interrupted"
        try:
            async for item in self._category_items(
                context, listing_url, policy, delay, state, counters
            ):
                counters["emitted"] += 1
                counters["parser_errors"] += int(item.error is not None)
                yield item
            status = "partial" if counters["parser_errors"] else "success"
        except Exception as exc:
            status = "failed"
            logger.error("category_failed", extra={"fields": {**fields, "error": str(exc)}})
            raise
        finally:
            logger.info(
                "category_finished",
                extra={
                    "fields": {
                        **fields,
                        **counters,
                        "status": status,
                        "locality": state.locality,
                    }
                },
            )

    async def _category_items(
        self,
        context: AdapterContext,
        listing_url: str,
        policy: RobotsPolicy,
        delay: float,
        state: _CoverageState,
        counters: dict[str, int],
    ) -> AsyncIterator[AcquisitionItem]:
        url: str | None = listing_url
        previous_page = 0
        category_seen: set[str] = set()
        for _ in range(self.settings.max_pages):
            if url is None:
                return
            page_number = self._page_number(url, listing_url)
            if page_number <= previous_page:
                raise ValueError("pagination cycle or backward page")
            previous_page = page_number
            if not policy.permits(url):
                raise ValueError(f"robots.txt disallows listing: {url}")
            response = await self._get(context, url, delay)
            counters["pages"] += 1
            fetched_at = utc_now()
            evidence = SourceEvidence(
                content=response.content,
                url=url,
                media_type="text/html",
                fetched_at=fetched_at,
                locator="document",
                metadata={
                    "source_id": self.source_id,
                    "http_status": response.status_code,
                    "category_slug": urlsplit(listing_url).path.rsplit("/", 1)[-1],
                },
            )
            try:
                response.raise_for_status()
                page = parse_page(response.content, url, fetched_at)
                if state.locality is not None and page.locality != state.locality:
                    raise ValueError("locality changed between pages")
            except (httpx.HTTPStatusError, ValueError) as exc:
                yield AcquisitionItem(evidence=evidence, error=f"listing failure: {exc}")
                raise
            state.locality = page.locality
            progress = 0
            for item in page.items:
                if item.candidate is None:
                    yield item
                    progress += 1
                    continue
                key = item.candidate["offer_key"]
                if key not in category_seen:
                    category_seen.add(key)
                    progress += 1
                # One campaign may be listed in several categories. First category wins;
                # compare substantive facts independently of that navigation classification.
                candidate = {
                    **item.candidate,
                    "product": {
                        k: v for k, v in item.candidate["product"].items() if k != "category"
                    },
                }
                fingerprint = hashlib.sha256(
                    json.dumps(candidate, sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest()
                if key in state.seen:
                    counters["duplicates"] += 1
                    if state.seen[key] != fingerprint:
                        yield AcquisitionItem(
                            evidence=item.evidence, error="conflicting repeated campaign"
                        )
                    continue
                state.seen[key] = fingerprint
                yield item
            if not progress and page.next_url is not None:
                raise ValueError("pagination made no progress")
            url = page.next_url
        if url is not None:
            raise ValueError("max_pages reached with more offers available; incomplete coverage")
