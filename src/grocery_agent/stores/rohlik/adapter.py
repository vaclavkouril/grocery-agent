import asyncio
import json
import logging
from collections.abc import AsyncIterator
from email.utils import parsedate_to_datetime

import httpx

from grocery_agent.models.common import utc_now
from grocery_agent.models.offer import Offer
from grocery_agent.stores.base import AcquisitionItem, AdapterContext, SourceEvidence, StoreAdapter
from grocery_agent.stores.robots import RobotsPolicy
from grocery_agent.stores.rohlik.config import RohlikSettings
from grocery_agent.stores.rohlik.parser import (
    BASE_URL,
    load_json,
    parse_cards,
    parse_context,
    parse_count,
    parse_ids,
)

logger = logging.getLogger(__name__)


class RohlikAdapter(StoreAdapter):
    def __init__(self, settings: RohlikSettings | None = None) -> None:
        self.settings = settings or RohlikSettings()

    @property
    def store_id(self) -> str:
        return "rohlik"

    def validate_offer(self, offer: Offer) -> None:
        super().validate_offer(offer)
        if (
            not offer.product.sku.isascii()
            or not offer.product.sku.isdigit()
            or offer.source_url.host != "www.rohlik.cz"
            or offer.source_url.path != f"/{offer.product.sku}"
            or not offer.scope.startswith("rohlik:online:warehouse:")
            or offer.offer_key not in {"standard", "loyalty"}
        ):
            raise ValueError("candidate identity does not belong to Rohlík")

    async def _get(
        self,
        context: AdapterContext,
        url: str,
        delay: float,
    ) -> httpx.Response:
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
        # Treat redirects as source failures, never follow an unexpected external destination.
        response.raise_for_status()
        return response

    async def fetch_offers(self, context: AdapterContext) -> AsyncIterator[AcquisitionItem]:
        delay = self.settings.request_delay_seconds
        robots = await self._get(context, f"{BASE_URL}/robots.txt", delay)
        policy = RobotsPolicy.parse(robots.text, context.http.headers.get("User-Agent", ""))
        delay = max(delay, policy.delay)

        async def get(url: str) -> httpx.Response:
            if not policy.permits(url):
                raise ValueError(f"robots.txt disallows catalog request: {url}")
            return await self._get(context, url, delay)

        home = await get(f"{BASE_URL}/")
        catalog = parse_context(home.content)
        if (
            self.settings.expected_warehouse_id is not None
            and catalog.warehouse_id != self.settings.expected_warehouse_id
        ):
            raise ValueError("public catalog differs from expected warehouse")
        if any(category not in catalog.categories for category in self.settings.category_ids):
            raise ValueError("configured category is missing from public navigation")
        seen: dict[tuple[str, str], str] = {}
        for category_id in self.settings.category_ids:
            category = catalog.categories[category_id]
            prefix = f"{BASE_URL}/api/v1/categories/normal/{category_id}/products"
            fields = {
                "source_id": self.source_id,
                "run_id": context.run_id,
                "category_id": category_id,
                "category": category,
                "scope": catalog.scope,
            }
            logger.info("category_started", extra={"fields": fields})
            count = parse_count((await get(prefix + "/count")).content)
            category_seen: set[int] = set()
            for page in range(self.settings.max_pages):
                if len(category_seen) == count:
                    break
                url = str(
                    httpx.URL(
                        prefix,
                        params={
                            "page": page,
                            "size": self.settings.page_size,
                            "sort": "price-asc",
                        },
                    )
                )
                response = await get(url)
                try:
                    ids = parse_ids(response.content, category_id)
                    if not ids or category_seen.intersection(ids):
                        raise ValueError("pagination made no progress or repeated products")
                    if len(ids) > self.settings.page_size or len(category_seen) + len(ids) > count:
                        raise ValueError("pagination disagrees with catalog count")
                except ValueError as exc:
                    yield AcquisitionItem(
                        evidence=SourceEvidence(
                            response.content,
                            url,
                            "application/json",
                            utc_now(),
                            "$",
                            {"source_id": self.source_id, "category_id": category_id},
                        ),
                        error=str(exc),
                    )
                    raise
                cards_url = str(
                    httpx.URL(
                        f"{BASE_URL}/api/v1/products/card",
                        params={
                            "products": ",".join(map(str, ids)),
                            "categoryType": "normal",
                        },
                    )
                )
                response = await get(cards_url)
                fetched_at = utc_now()
                try:
                    cards = load_json(response.content)
                    if (
                        not isinstance(cards, list)
                        or len(cards) != len(ids)
                        or any(not isinstance(card, dict) for card in cards)
                        or {card.get("productId") for card in cards} != set(ids)
                    ):
                        raise ValueError("product card response does not cover requested IDs")
                    items = parse_cards(response.content, cards_url, fetched_at, catalog, category)
                except (ValueError, TypeError) as exc:
                    yield AcquisitionItem(
                        evidence=SourceEvidence(
                            response.content,
                            cards_url,
                            "application/json",
                            fetched_at,
                            "$",
                            {"source_id": self.source_id, "category_id": category_id},
                        ),
                        error=str(exc),
                    )
                    raise
                category_seen.update(ids)
                for item in items:
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
                logger.info(
                    "category_page_finished",
                    extra={
                        "fields": {
                            **fields,
                            "page": page,
                            "products_seen": len(category_seen),
                            "total": count,
                        }
                    },
                )
            if len(category_seen) != count:
                raise ValueError("max_pages reached; incomplete category coverage")
            if parse_count((await get(prefix + "/count")).content) != count:
                raise ValueError("catalog count changed during acquisition; incomplete coverage")
            logger.info("category_finished", extra={"fields": {**fields, "products": count}})
        final = parse_context((await get(f"{BASE_URL}/")).content)
        if final.scope != catalog.scope:
            raise ValueError("warehouse/locality changed during acquisition")
