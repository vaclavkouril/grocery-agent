from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from bs4 import BeautifulSoup
from pydantic import ValidationError
from sqlalchemy import func, select

from grocery_agent.matching.base import ExactGTINResolver
from grocery_agent.models.offer import Offer
from grocery_agent.persistence.database import open_database
from grocery_agent.persistence.repository import SQLAlchemyOfferRepository
from grocery_agent.persistence.schema import (
    ObservationRow,
    ScrapeRunRow,
    SnapshotRow,
    StoreProductRow,
)
from grocery_agent.persistence.snapshots import FileSnapshotStore
from grocery_agent.pipeline.service import ScrapePipeline
from grocery_agent.stores.base import AdapterContext
from grocery_agent.stores.kupi.config import KupiSettings
from grocery_agent.stores.kupi.normalization import czech_decimal, parse_quantity, parse_validity
from grocery_agent.stores.kupi.parser import parse_page
from grocery_agent.stores.kupi.robots import RobotsPolicy
from tests.kupi_support import FETCHED_AT, FIXTURES, LISTING_URL, adapter, fixture_response


def first_page():
    return parse_page((FIXTURES / "page1.html").read_bytes(), LISTING_URL, FETCHED_AT)


def test_actual_source_fixture_models_and_retailer_boundary() -> None:
    page = first_page()
    offers = [Offer.model_validate(item.candidate) for item in page.items]
    assert len(offers) == 54 and page.locality == "Praha"
    assert page.next_url == LISTING_URL + "?page=2"
    assert {o.product.store_id for o in offers} >= {"tesco", "lidl", "billa", "kosik", "albert"}
    assert all(o.scope == "kupi:locality:praha" for o in offers)
    assert all(o.regular_price is None and o.discount_percent is None for o in offers)
    assert all(o.availability == "unknown" for o in offers)
    assert all(o.product.gtin is None for o in offers)
    for offer in offers:
        adapter().validate_offer(offer)


def test_loyalty_pack_and_mass_comparisons() -> None:
    offers = {
        o.offer_key: o for o in map(lambda i: Offer.model_validate(i.candidate), first_page().items)
    }
    blueberry = offers["kupi:11100162"]
    assert blueberry.current_price == Decimal("24.90")
    assert blueberry.unit_price.amount == Decimal("199.2")
    assert blueberry.unit_price.unit == "kg"
    assert blueberry.product.quantity.amount == Decimal("125")
    assert blueberry.promotion.requires_loyalty
    assert blueberry.promotion.advertised_discount_percent == Decimal("44")
    assert blueberry.promotion.discount_reference == "unspecified"
    assert blueberry.valid_from == date(2026, 10, 1)
    assert blueberry.valid_until == date(2026, 10, 4)
    lettuce = offers["kupi:11114883"]
    assert lettuce.product.quantity.amount == 2
    assert lettuce.unit_price.unit == "piece"
    assert lettuce.unit_price.amount == lettuce.current_price / 2
    bananas = offers["kupi:11108694"]
    assert bananas.price_basis.unit == "kg" and bananas.price_basis.amount == 1
    assert bananas.product.quantity is None  # Do not claim a known package from a per-kg quote.


def test_parser_rejects_one_broken_row_without_dropping_neighbors() -> None:
    soup = BeautifulSoup((FIXTURES / "page1.html").read_bytes(), "lxml")
    del soup.select_one(".discount_row")["data-product"]
    items = parse_page(str(soup).encode(), LISTING_URL, FETCHED_AT).items
    assert len(items) == 54 and items[0].error
    assert all(item.candidate for item in items[1:])
    assert items[0].evidence.content == str(soup).encode()


@pytest.mark.parametrize("content", [b"Access denied", b"<h1>captcha</h1>", b"<html></html>"])
def test_parser_rejects_unrecognized_source(content: bytes) -> None:
    with pytest.raises(ValueError):
        parse_page(content, LISTING_URL, FETCHED_AT)


@pytest.mark.parametrize(
    "value,expected", [("24,90 Kč", "24.90"), ("1\xa0299,00", "1299.00"), ("0.5", "0.5")]
)
def test_czech_money(value: str, expected: str) -> None:
    assert czech_decimal(value) == Decimal(expected)


@pytest.mark.parametrize("value", ["NaN", "-", "24 až 30", "1,2,3", "", "inf"])
def test_invalid_money(value: str) -> None:
    with pytest.raises(ValueError):
        czech_decimal(value)


@pytest.mark.parametrize(
    "value,amount,unit",
    [
        ("/ 0.5 kg", "0.5", "kg"),
        ("/ 125 g", "125", "g"),
        ("/ 2 ks", "2", "piece"),
        ("/ 1 bal", "1", "package"),
        ("/ 500 ml", "500", "ml"),
    ],
)
def test_quantity(value: str, amount: str, unit: str) -> None:
    result = parse_quantity(value)
    assert result.amount == Decimal(amount) and result.unit == unit


@pytest.mark.parametrize("value", ["/ 0 kg", "/ 1 box", "", "/ -2 ks", "/ kg"])
def test_invalid_quantity(value: str) -> None:
    with pytest.raises(ValueError):
        parse_quantity(value)


@pytest.mark.parametrize(
    "text,today,start,end",
    [
        ("čt 1. 10. – ne 4. 10.", date(2026, 9, 30), date(2026, 10, 1), date(2026, 10, 4)),
        ("platí do úterý 6. 10.", date(2026, 9, 30), None, date(2026, 10, 6)),
        ("dnes končí", date(2026, 9, 30), None, date(2026, 9, 30)),
        ("aktuální", date(2026, 9, 30), None, None),
        ("28. 12. – 4. 1.", date(2026, 12, 30), date(2026, 12, 28), date(2027, 1, 4)),
        ("28. 12. – 4. 1.", date(2027, 1, 2), date(2026, 12, 28), date(2027, 1, 4)),
        ("platí do 29. 2.", date(2028, 2, 1), None, date(2028, 2, 29)),
    ],
)
def test_validity(text: str, today: date, start: date | None, end: date | None) -> None:
    assert parse_validity(text, today) == (start, end)


@pytest.mark.parametrize("value", ["31. 2. – 3. 3.", "4. 10. – 1. 10.", "brzy", "1. 10."])
def test_invalid_validity(value: str) -> None:
    with pytest.raises(ValueError):
        parse_validity(value, date(2026, 9, 30))


def test_robots_matches_actual_public_rules() -> None:
    policy = RobotsPolicy.parse((FIXTURES / "robots.txt").read_text(), "grocery-agent/0.1")
    assert policy.permits(LISTING_URL)
    assert policy.permits(LISTING_URL + "?page=2")
    assert not policy.permits("https://www.kupi.cz/get-slevy")
    assert not policy.permits("https://www.kupi.cz/popup/discount")
    assert not policy.permits("https://www.kupi.cz/redir.php?hash=x")
    assert not policy.permits(LISTING_URL + "?ord=price")
    assert not RobotsPolicy.parse((FIXTURES / "robots.txt").read_text(), "AI").permits(LISTING_URL)


def test_robots_priority_and_terminal_anchor() -> None:
    policy = RobotsPolicy.parse("User-agent: *\nDisallow: /x*\nAllow: /xyz$\n", "scraper")
    assert policy.permits("https://www.kupi.cz/xyz")
    assert not policy.permits("https://www.kupi.cz/xyz?x=1")


@pytest.mark.parametrize(
    "url",
    [
        "http://www.kupi.cz/slevy/a",
        "https://evil.cz/slevy/a",
        "https://www.kupi.cz/get-slevy",
        "https://www.kupi.cz/slevy/../get-slevy",
        LISTING_URL + "?ord=price",
    ],
)
def test_config_restricts_to_public_categories(url: str) -> None:
    with pytest.raises(ValidationError):
        KupiSettings(listing_url=url)


async def test_paginated_stream_deduplicates_repeated_campaigns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("grocery_agent.stores.kupi.adapter.utc_now", lambda: FETCHED_AT)
    requests = []

    def transport(request):
        requests.append(str(request.url))
        return fixture_response(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        items = [i async for i in adapter().fetch_offers(AdapterContext(client))]
    assert len(items) == 90 and all(item.error is None for item in items)
    assert requests == ["https://www.kupi.cz/robots.txt", LISTING_URL, LISTING_URL + "?page=2"]


async def test_pipeline_retains_evidence_and_repeated_scrape_has_no_new_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("grocery_agent.stores.kupi.adapter.utc_now", lambda: FETCHED_AT)
    engine = open_database(f"sqlite:///{tmp_path / 'test.db'}")
    repo = SQLAlchemyOfferRepository(engine)
    pipeline = ScrapePipeline(repo, FileSnapshotStore(tmp_path / "snapshots"), ExactGTINResolver())
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture_response)) as client:
        result = await pipeline.run(adapter(), AdapterContext(client))
        assert (result.source_id, result.status, result.accepted, result.changed) == (
            "kupi",
            "success",
            90,
            90,
        )
        repeated = await pipeline.run(adapter(), AdapterContext(client))
        assert repeated.status == "success" and repeated.changed == 0
    with engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(ObservationRow)) == 90
        assert conn.scalar(select(func.count()).select_from(SnapshotRow)) == 180
        assert conn.scalar(select(ScrapeRunRow.source_id)) == "kupi"
        retailers = set(conn.scalars(select(StoreProductRow.store_id)))
        assert "kupi" not in retailers and {"tesco", "lidl", "billa"} <= retailers
    raw_files = list((tmp_path / "snapshots").rglob("*.bin"))
    assert len(raw_files) == 2
    assert {path.read_bytes() for path in raw_files} == {
        (FIXTURES / "page1.html").read_bytes(),
        (FIXTURES / "page2.html").read_bytes(),
    }
    assert repo.recent_runs(2)[0]["source_id"] == "kupi"
    engine.dispose()


@pytest.mark.parametrize(
    "failure", ["denied", "markup", "limit", "cycle", "offsite", "locality", "robots"]
)
async def test_source_failures_are_visible_and_retained(
    failure: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("grocery_agent.stores.kupi.adapter.utc_now", lambda: FETCHED_AT)

    def transport(request):
        if request.url.path == "/robots.txt" and failure == "robots":
            return httpx.Response(200, text="User-agent: *\nDisallow: /slevy/\n")
        if request.url.path == "/robots.txt":
            return fixture_response(request)
        if failure == "denied":
            return httpx.Response(403, text="Access Denied")
        if failure == "markup":
            return httpx.Response(200, text="captcha")
        body = (FIXTURES / "page1.html").read_text()
        if failure == "cycle":
            body = body.replace("?page=2", "?page=1")
        if failure == "offsite":
            body = body.replace("/slevy/ovoce-a-zelenina?page=2", "https://evil.cz/slevy/x")
        if failure == "locality" and request.url.params.get("page") == "2":
            body = body.replace("Praha", "Brno")
        return httpx.Response(200, text=body)

    engine = open_database(f"sqlite:///{tmp_path / 'test.db'}")
    repo = SQLAlchemyOfferRepository(engine)
    pipeline = ScrapePipeline(repo, FileSnapshotStore(tmp_path / "raw"), ExactGTINResolver())
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = await pipeline.run(
            adapter(max_pages=1 if failure == "limit" else 3), AdapterContext(client)
        )
    assert result.status == "failed" and result.errors == 1
    if failure in {"denied", "markup", "locality"}:
        assert result.rejected >= 1
        with engine.connect() as conn:
            assert conn.scalar(select(func.count()).select_from(SnapshotRow)) >= 1
    engine.dispose()


async def test_transient_request_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_wait(delay):
        return None

    monkeypatch.setattr("grocery_agent.stores.kupi.adapter.asyncio.sleep", no_wait)
    monkeypatch.setattr("grocery_agent.stores.kupi.adapter.utc_now", lambda: FETCHED_AT)
    count = 0

    def transport(request):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(503)
        if count == 2:
            raise httpx.ReadTimeout("timeout", request=request)
        return fixture_response(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        items = [i async for i in adapter().fetch_offers(AdapterContext(client))]
    assert count == 5 and len(items) == 90


@pytest.mark.parametrize("header", ["120", "Wed, 30 Sep 2026 12:02:00 GMT"])
async def test_long_retry_after_fails_without_early_retry(
    header: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("grocery_agent.stores.kupi.adapter.utc_now", lambda: FETCHED_AT)
    requests = []

    def transport(request):
        requests.append(str(request.url))
        return httpx.Response(429, headers={"Retry-After": header})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            _ = [item async for item in adapter().fetch_offers(AdapterContext(client))]
    assert requests == ["https://www.kupi.cz/robots.txt"]


@pytest.mark.parametrize("price,percent", [("-1 Kč", "–44 %"), ("24,90 Kč", "–101 %")])
async def test_corrupt_price_or_discount_is_rejected_before_sql(
    price: str, percent: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("grocery_agent.stores.kupi.adapter.utc_now", lambda: FETCHED_AT)
    soup = BeautifulSoup((FIXTURES / "page1.html").read_bytes(), "lxml")
    row = soup.select_one(".discount_row")
    row.select_one(".discount_price_value").string = price
    row.select_one(".discount_percentage").string = percent
    for link in soup.select("a.load_discounts"):
        link.decompose()

    def transport(request):
        if request.url.path == "/robots.txt":
            return fixture_response(request)
        return httpx.Response(200, text=str(soup))

    engine = open_database(f"sqlite:///{tmp_path / 'test.db'}")
    pipeline = ScrapePipeline(
        SQLAlchemyOfferRepository(engine), FileSnapshotStore(tmp_path / "raw"), ExactGTINResolver()
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = await pipeline.run(adapter(), AdapterContext(client))
    assert (result.status, result.rejected, result.accepted, result.errors) == ("partial", 1, 50, 0)
    with engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(ObservationRow)) == 50
    engine.dispose()
