import json
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine

from grocery_agent.matching.base import ExactGTINResolver
from grocery_agent.models.offer import Offer
from grocery_agent.persistence.reader import SQLAlchemyCurrentOfferReader
from grocery_agent.persistence.repository import SQLAlchemyOfferRepository
from grocery_agent.persistence.snapshots import FileSnapshotStore
from grocery_agent.pipeline.service import ScrapePipeline
from grocery_agent.stores.base import AdapterContext
from grocery_agent.stores.rohlik.config import RohlikSettings
from grocery_agent.stores.rohlik.parser import (
    load_json,
    parse_cards,
    parse_context,
    parse_count,
    parse_ids,
)
from tests.rohlik_support import FETCHED_AT, FIXTURES, adapter, fixture_response


def cards() -> list[dict[str, Any]]:
    return load_json((FIXTURES / "cards.json").read_bytes())  # type: ignore[no-any-return]


def parse(records: list[Any]) -> list[Offer]:
    # Serialize test changes without losing their exact numeric text.
    content = json.dumps(records, default=str).encode()
    items = parse_cards(
        content,
        "https://www.rohlik.cz/api/v1/products/card",
        FETCHED_AT,
        parse_context((FIXTURES / "context.html").read_bytes()),
        "Ovoce a zelenina",
    )
    assert all(i.error is None for i in items), [i.error for i in items]
    return [Offer.model_validate(i.candidate) for i in items]


def test_public_context_and_exact_prices() -> None:
    context = parse_context((FIXTURES / "context.html").read_bytes())
    assert context.scope == "rohlik:online:warehouse:8791:locality:praha-7"
    assert context.categories[300102000] == "Ovoce a zelenina"
    offers = parse(cards())
    assert len(offers) == 9
    assert offers[0].current_price == Decimal("20.9")
    assert offers[0].product.gtin is None


def test_variable_weight_uses_quoted_kg_price_not_approximate_item_price() -> None:
    card = cards()[1]
    offer = parse([card])[0]
    assert offer.current_price == card["prices"]["unitPrice"] == Decimal("134.9")
    assert offer.price_basis.unit == "kg" and offer.price_basis.amount == 1
    assert offer.product.variable_weight and offer.product.quantity is None
    assert offer.unit_price.amount == Decimal("134.9000")


def test_variable_weight_sale_does_not_invent_regular_mass_price() -> None:
    offer = parse([cards()[4]])[0]
    assert offer.regular_price is None and offer.promotion is not None
    assert offer.promotion.kind == "advertised"
    assert offer.promotion.discount_reference == "unspecified"
    assert offer.promotion.advertised_discount_percent == 25
    assert offer.current_price == Decimal("62.17")
    assert str(offer.valid_until) == "2026-10-01"


def test_conditional_prices_are_separate_from_standard_quotes() -> None:
    standard, loyalty = parse([cards()[3]])
    assert standard.offer_key == "standard" and standard.current_price == Decimal("21.9")
    assert standard.promotion is None
    assert loyalty.offer_key == "loyalty" and loyalty.current_price == Decimal("19.71")
    assert loyalty.promotion and loyalty.promotion.requires_loyalty
    assert standard.unit_price.unit == loyalty.unit_price.unit == "piece"
    # No unconditional mass price can be recovered for a weighted loyalty offer.
    weighted = parse([cards()[2]])[0]
    assert weighted.offer_key == "loyalty" and weighted.regular_price is None


def test_fixed_pack_multipack_sale_and_unknown_size() -> None:
    card = cards()[0]
    card.update(textualAmount="6 × 250 ml", unit="l", brand="Example")
    card["prices"].update(originalPrice="90", salePrice="75", unitPrice="50", saleText="-17 %")
    offer = parse([card])[0]
    assert offer.product.quantity and offer.product.quantity.amount == 1500
    assert offer.unit_price.unit == "l" and offer.unit_price.amount == 50
    assert offer.regular_price == 90 and offer.promotion and offer.promotion.kind == "price_cut"
    card["textualAmount"] = "cca 1 kg"
    assert parse([card])[0].product.quantity is None


def test_stock_and_sale_expiry_are_conservative() -> None:
    card = cards()[4]
    card["stock"]["availabilityStatus"] = "SOLD_OUT"
    card["prices"]["saleValidTill"] = "2026-10-02T10:01:00+02:00"
    offer = parse([card])[0]
    assert offer.availability == "unavailable" and str(offer.valid_until) == "2026-10-01"
    card["stock"]["availabilityStatus"] = "NEW_UNKNOWN_STATUS"
    assert parse([card])[0].availability == "unknown"


@pytest.mark.parametrize(
    "change",
    [
        {"prices": {"currency": "EUR"}},
        {"weightedItem": "false"},
        {"productId": True},
        {"name": ""},
        {"prices": {"currency": "CZK", "originalPrice": -1}},
    ],
)
def test_record_errors_retain_evidence_and_continue(change: dict[str, Any]) -> None:
    malformed = {**cards()[0], **change}
    content = json.dumps([malformed, cards()[0]], default=str).encode()
    context = parse_context((FIXTURES / "context.html").read_bytes())
    items = parse_cards(
        content,
        "https://www.rohlik.cz/api/v1/products/card",
        FETCHED_AT,
        context,
        "Ovoce a zelenina",
    )
    assert items[0].error and items[1].candidate
    assert items[0].evidence.content == content and items[0].evidence.locator == "$[0]"


def test_unknown_promotion_conditions_are_rejected() -> None:
    card = cards()[0]
    card["prices"].update(salePrice="10", saleText="2+1 zdarma")
    context = parse_context((FIXTURES / "context.html").read_bytes())
    items = parse_cards(
        json.dumps([card], default=str).encode(),
        "https://www.rohlik.cz/",
        FETCHED_AT,
        context,
        "Ovoce a zelenina",
    )
    assert items[0].error and "conditions" in items[0].error


@pytest.mark.parametrize("content", [b"{}", b"{broken", b'[{"status":400}]'])
def test_broken_sources_raise(content: bytes) -> None:
    with pytest.raises(ValueError):
        parse_context(content)
    with pytest.raises(ValueError):
        parse_count(content)
    with pytest.raises(ValueError):
        parse_ids(content, 300102000)


@pytest.mark.parametrize(
    "values",
    [
        {"category_ids": []},
        {"category_ids": [1, 1]},
        {"category_ids": [-1]},
        {"page_size": 101},
        {"max_pages": 0},
        {"expected_warehouse_id": 0},
    ],
)
def test_configuration_bounds(values: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        RohlikSettings.model_validate(values)


async def collect(transport: Any, **settings: Any) -> list[Any]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        return [i async for i in adapter(**settings).fetch_offers(AdapterContext(client))]


async def test_adapter_covers_both_pages_and_checks_final_context() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return fixture_response(request)

    items = await collect(handler)
    assert len(items) == 9 and all(i.candidate for i in items)
    assert [r.url.params["page"] for r in requests if r.url.path.endswith("/products")] == [
        "0",
        "1",
    ]
    assert sum(r.url.path == "/" for r in requests) == 2
    assert all(i.evidence.media_type == "application/json" for i in items)


@pytest.mark.parametrize(
    "mode,match",
    [
        ("cycle", "repeated"),
        ("missing", "cover requested"),
        ("count_change", "count changed"),
        ("scope_change", "warehouse/locality changed"),
        ("robots", "disallows"),
        ("broken_json", "Expecting"),
    ],
)
async def test_incomplete_catalog_never_succeeds(mode: str, match: str) -> None:
    counts = 0
    homes = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal counts, homes
        if request.url.path == "/robots.txt" and mode == "robots":
            return httpx.Response(200, text="User-agent: *\nDisallow: /api/\n")
        response = fixture_response(request)
        if request.url.path.endswith("/products") and request.url.params["page"] == "1":
            if mode == "cycle":
                return fixture_response(
                    httpx.Request("GET", str(request.url).replace("page=1", "page=0"))
                )
        if request.url.path == "/api/v1/products/card":
            if mode == "missing":
                return httpx.Response(200, json=[])
            if mode == "broken_json":
                return httpx.Response(200, text="{broken")
        if request.url.path.endswith("/count"):
            counts += 1
            if mode == "count_change" and counts == 2:
                return httpx.Response(200, json={"results": 9})
        if request.url.path == "/":
            homes += 1
            if mode == "scope_change" and homes == 2:
                return httpx.Response(
                    200,
                    content=response.content.replace(
                        b'"warehouse_id": 8791', b'"warehouse_id": 8793'
                    ),
                )
        return response

    with pytest.raises(ValueError, match=match):
        await collect(handler)


async def test_max_pages_and_expected_warehouse() -> None:
    with pytest.raises(ValueError, match="max_pages"):
        await collect(fixture_response, max_pages=1)
    with pytest.raises(ValueError, match="expected warehouse"):
        await collect(fixture_response, expected_warehouse_id=8793)
    with pytest.raises(ValueError, match="missing from public navigation"):
        await collect(fixture_response, category_ids=[42])


async def test_access_denial_stops_requests() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(403)

    with pytest.raises(httpx.HTTPStatusError):
        await collect(handler, attempts=3)
    assert requests == ["/robots.txt"]


async def test_transient_retry_obeys_server_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = 0
    sleeps = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("grocery_agent.stores.rohlik.adapter.asyncio.sleep", sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return fixture_response(request)

    assert len(await collect(handler, attempts=2)) == 9
    assert 7 in sleeps


async def test_identity_boundary() -> None:
    offer = parse(cards())[0]
    payload = offer.model_dump(exclude_computed_fields=True)
    for changed in [
        {**payload, "scope": "national"},
        {**payload, "product": {**payload["product"], "store_id": "tesco"}},
        {**payload, "source_url": "https://example.com/1"},
    ]:
        # Validate into a proper HttpUrl before applying the boundary.
        with pytest.raises(ValueError):
            adapter().validate_offer(Offer.model_validate(changed))


async def test_rohlik_persistence_and_unchanged_history(
    engine: Engine,
    repository: SQLAlchemyOfferRepository,
    snapshots: FileSnapshotStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("grocery_agent.stores.rohlik.adapter.utc_now", lambda: FETCHED_AT)
    pipeline = ScrapePipeline(repository, snapshots, ExactGTINResolver())
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture_response)) as client:
        first = await pipeline.run(adapter(), AdapterContext(client))
        second = await pipeline.run(adapter(), AdapterContext(client))
    assert first.status == second.status == "success"
    assert first.accepted == first.changed == second.accepted == 9
    assert second.changed == 0
    reader = SQLAlchemyCurrentOfferReader(engine)
    offers = list(reader.iter_offers(reader.latest_batch("rohlik")))
    assert len(offers) == 9
    assert {i.offer.product.store_id for i in offers} == {"rohlik"}
    assert {i.offer.scope for i in offers} == {"rohlik:online:warehouse:8791:locality:praha-7"}


def test_advertised_price_without_original_price() -> None:
    card = cards()[0]
    card["prices"].update(originalPrice=None, salePrice="44.9", saleText=None)
    offer = parse([card])[0]
    assert offer.current_price == Decimal("44.9") and offer.regular_price is None
    assert offer.promotion and offer.promotion.kind == "advertised"
    assert offer.promotion.discount_reference == "unspecified"


async def test_overlapping_categories_deduplicate_identical_offers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        response = fixture_response(request)
        if request.url.path.startswith("/api/v1/categories/normal/300103000/"):
            data = response.json()
            if "categoryId" in data:
                data["categoryId"] = 300103000
            return httpx.Response(200, json=data)
        return response

    assert len(await collect(handler, category_ids=[300102000, 300103000])) == 9
