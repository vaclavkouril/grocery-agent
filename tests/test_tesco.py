import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import httpx
import pytest
from bs4 import BeautifulSoup
from sqlalchemy import Engine

from grocery_agent.matching.base import ExactGTINResolver
from grocery_agent.models.common import utc_now
from grocery_agent.models.offer import Offer
from grocery_agent.persistence.reader import SQLAlchemyCurrentOfferReader
from grocery_agent.persistence.repository import SQLAlchemyOfferRepository
from grocery_agent.persistence.snapshots import FileSnapshotStore
from grocery_agent.pipeline.service import ScrapePipeline
from grocery_agent.stores.base import AdapterContext
from grocery_agent.stores.tesco.config import TescoSettings
from grocery_agent.stores.tesco.parser import (
    SCOPE,
    discover_categories,
    page_number,
    parse_page,
    public_snapshot,
)
from tests.tesco_support import FIXTURES, LISTING_URL, adapter, fixture_response


def source() -> bytes:
    return (FIXTURES / "produce.html").read_bytes()


def parse(content: bytes | None = None) -> list[Offer]:
    page = parse_page(content or source(), LISTING_URL, utc_now(), "Ovoce a zelenina")
    assert not any(i.error for i in page.items), [i.error for i in page.items]
    return [Offer.model_validate(i.candidate) for i in page.items]


def test_captured_catalog_contains_ordinary_prices_and_clubcard_variants() -> None:
    offers = parse()
    assert len(offers) == 28
    assert {o.product.store_id for o in offers} == {"tesco"}
    banana = next(o for o in offers if o.product.sku == "212690126")
    assert banana.current_price == Decimal("39.90")
    assert banana.price_basis.unit == "kg" and banana.product.variable_weight
    assert banana.product.quantity is None and banana.product.gtin is None
    assert banana.scope == SCOPE
    raspberry = [o for o in offers if o.product.sku == "212693561"]
    assert {o.offer_key for o in raspberry} == {"standard", "loyalty"}
    assert raspberry[0].unit_price.amount == Decimal("479.2")
    assert raspberry[1].unit_price.amount == Decimal("455.2")
    assert raspberry[1].promotion and raspberry[1].promotion.requires_loyalty
    assert raspberry[1].product.quantity and raspberry[1].product.quantity.amount == 125
    garlic = next(o for o in offers if o.product.sku == "212693462" and o.offer_key == "loyalty")
    assert garlic.current_price == 99 and garlic.price_basis.unit == "kg"


def test_public_department_discovery_and_pagination() -> None:
    departments = discover_categories(source())
    assert len(departments) == 13
    assert {"trvanlive", "pekarna", "maso-a-lahudky", "domov-a-zabava"} <= departments.keys()
    assert "top-vyber" not in departments and "novinky" not in departments
    page = parse_page(source(), LISTING_URL, utc_now(), "Ovoce a zelenina")
    assert (page.start, page.end, page.total) == (1, 24, 393)
    assert page.next_url == LISTING_URL + "?sortBy=relevance&page=2&count=24"


def test_reduced_snapshots_remove_session_data_and_preserve_offers() -> None:
    soup = BeautifulSoup(source(), "lxml")
    state = soup.new_tag("script", type="application/discover+json")
    state.string = json.dumps(
        {
            "mfe-plp": {
                "props": {
                    "authentication": {
                        "authenticated": False,
                        "UUID": "session-secret",
                    }
                }
            }
        }
    )
    tracker = soup.new_tag("script")
    tracker.string = "tracking-token"
    assert soup.body
    soup.body.extend([state, tracker])
    content = public_snapshot(str(soup).encode())
    assert b"session-secret" not in content and b"tracking-token" not in content
    assert parse(content) == parse()
    state.string = state.string.replace("false", "true")
    with pytest.raises(ValueError, match="anonymous"):
        public_snapshot(str(soup).encode())


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/x?page=2",
        LISTING_URL + "?page=0",
        LISTING_URL + "?page=x",
        LISTING_URL + "?page=2&page=3",
        LISTING_URL + "?offers=1",
        LISTING_URL + "?sortBy=price-ascending",
        LISTING_URL + "?count=1000",
        LISTING_URL.replace("/all", "/somewhere"),
    ],
)
def test_pagination_cannot_escape_the_unfiltered_category(url: str) -> None:
    with pytest.raises(ValueError):
        page_number(url, LISTING_URL)


def test_malformed_tile_retains_evidence_and_other_products() -> None:
    soup = BeautifulSoup(source(), "lxml")
    price = soup.select_one('[class*="product-tile-price__text"]')
    assert price
    price.string = "broken"
    content = str(soup).encode()
    page = parse_page(content, LISTING_URL, utc_now(), "Ovoce a zelenina")
    assert page.items[0].error and page.items[1].candidate
    assert page.items[0].evidence.content == content
    assert "212690126" in page.items[0].evidence.locator


@pytest.mark.parametrize("content", [b"Access Denied", b"<html></html>", b"{broken"])
def test_missing_page_context_fails_visibly(content: bytes) -> None:
    with pytest.raises(ValueError):
        public_snapshot(content)
    with pytest.raises(ValueError):
        parse_page(content, LISTING_URL, utc_now(), "Ovoce a zelenina")


async def collect(handler: Callable[[httpx.Request], httpx.Response], **settings: Any) -> list[Any]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        return [i async for i in adapter(**settings).fetch_offers(AdapterContext(http))]


async def test_complete_two_page_catalog() -> None:
    items = await collect(fixture_response)
    assert len(items) == 28 and all(i.candidate for i in items)


async def test_access_denial_stops_without_retry_or_empty_success() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url)
        if "page" in request.url.params:
            return httpx.Response(403)
        return fixture_response(request)

    with pytest.raises(httpx.HTTPStatusError):
        await collect(handler, attempts=3)
    assert len(requests) == 3


@pytest.mark.parametrize(
    "mode,match",
    [
        ("count", "total changed"),
        ("cycle", "repeated, skipped"),
        ("missing_next", "missing or ambiguous"),
        ("robots", "disallows"),
    ],
)
async def test_incomplete_catalog_is_a_failure(mode: str, match: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if mode == "robots" and request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /shop/\n")
        response = fixture_response(request)
        if "page" in request.url.params:
            if mode == "count":
                soup = BeautifulSoup(response.content, "lxml")
                count = soup.select_one('[data-testid="pagination-result-count"]')
                assert count and soup.body
                count.string = "Zobrazeno 13 až 24 z 25 produktů"
                next_link = soup.new_tag(
                    "a", href=LISTING_URL + "?sortBy=relevance&page=3&count=24"
                )
                next_link.string = "3"
                soup.body.append(next_link)
                return httpx.Response(200, content=str(soup).encode())
            if mode == "cycle":
                soup = BeautifulSoup(
                    fixture_response(httpx.Request("GET", LISTING_URL)).content, "lxml"
                )
                count = soup.select_one('[data-testid="pagination-result-count"]')
                assert count
                count.string = "Zobrazeno 13 až 24 z 24 produktů"
                for anchor in soup.find_all("a", href=True):
                    if "?" in str(anchor["href"]):
                        anchor.decompose()
                return httpx.Response(200, content=str(soup).encode())
        elif mode == "missing_next" and request.url.path.endswith("/all"):
            soup = BeautifulSoup(response.content, "lxml")
            for anchor in soup.find_all("a", href=True):
                if "page=2" in str(anchor["href"]):
                    anchor.decompose()
            return httpx.Response(200, content=str(soup).encode())
        return response

    with pytest.raises(ValueError, match=match):
        await collect(handler)


async def test_page_cap_and_unknown_department() -> None:
    with pytest.raises(ValueError, match="max_pages"):
        await collect(fixture_response, max_pages=1)


@pytest.mark.parametrize(
    "settings",
    [
        {"category_slugs": []},
        {"category_slugs": ["a", "a"]},
        {"category_slugs": ["../x"]},
        {"category_slugs": ["top-vyber"]},
        {"max_pages": 0},
        {"attempts": 0},
    ],
)
def test_configuration_bounds(settings: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        TescoSettings.model_validate(settings)


async def test_pipeline_history_and_failed_batches(
    engine: Engine,
    repository: SQLAlchemyOfferRepository,
    snapshots: FileSnapshotStore,
) -> None:
    pipeline = ScrapePipeline(repository, snapshots, ExactGTINResolver())
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture_response)) as http:
        first = await pipeline.run(adapter(), AdapterContext(http))
        second = await pipeline.run(adapter(), AdapterContext(http))
    assert first.status == second.status == "success"
    assert first.accepted == first.changed == second.accepted == 28 and second.changed == 0
    reader = SQLAlchemyCurrentOfferReader(engine)
    offers = list(reader.iter_offers(reader.latest_batch("tesco")))
    assert len(offers) == 28
    assert {i.offer.scope for i in offers} == {SCOPE}
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture_response)) as http:
        failed = await pipeline.run(adapter(max_pages=1), AdapterContext(http))
    assert failed.status == "failed" and failed.accepted > 0
    with pytest.raises(ValueError, match="unsuccessful"):
        reader.latest_batch("tesco")


def test_fixed_pack_title_describes_total_multipack_contents() -> None:
    soup = BeautifulSoup(source(), "lxml")
    tile = soup.select("#list-content > li")[14]
    heading = tile.select_one("h2 a")
    assert heading
    heading.string = "Example juice 6 × 500 ml"
    offer = next(o for o in parse(str(soup).encode()) if o.product.sku == "212693561")
    assert offer.product.quantity and offer.product.quantity.amount == 3000
    assert offer.unit_price.unit == "l"
    heading.string = "Example loose pack cca 500 g"
    offer = next(o for o in parse(str(soup).encode()) if o.product.sku == "212693561")
    assert offer.product.quantity is None


def test_clubcard_mass_quote_converts_fixed_gram_pack_contents() -> None:
    soup = BeautifulSoup(source(), "lxml")
    tile = soup.select("#list-content > li")[14]
    label = tile.select_one(".ddsweb-value-bar__content-text")
    assert label
    label.string = "400 Kč/kg s Clubcard"
    offer = next(
        o
        for o in parse(str(soup).encode())
        if o.product.sku == "212693561" and o.offer_key == "loyalty"
    )
    assert offer.current_price == 50 and offer.unit_price.amount == 400
