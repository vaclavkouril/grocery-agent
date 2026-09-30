import logging
from decimal import Decimal

import httpx
import pytest
from bs4 import BeautifulSoup

from grocery_agent.matching.base import ExactGTINResolver
from grocery_agent.models.offer import Offer
from grocery_agent.pipeline.service import ScrapePipeline
from grocery_agent.stores.base import AdapterContext
from grocery_agent.stores.kupi.adapter import KupiAdapter
from grocery_agent.stores.kupi.config import KupiSettings
from grocery_agent.stores.kupi.normalization import parse_quantity, parse_validity
from grocery_agent.stores.kupi.parser import parse_page
from tests.adapter_contract import assert_adapter_contract
from tests.kupi_support import FETCHED_AT, FIXTURES, LISTING_URL, adapter, fixture_response

GROCERIES = FIXTURES / "groceries"
DEFAULT_CATEGORIES = KupiSettings(_env_file=None).category_slugs


def grocery_response(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/robots.txt":
        return fixture_response(request)
    category = request.url.path.removeprefix("/slevy/")
    assert category in DEFAULT_CATEGORIES and not request.url.query
    return httpx.Response(200, content=(GROCERIES / f"{category}.html").read_bytes())


@pytest.mark.parametrize("category", DEFAULT_CATEGORIES)
def test_saved_real_rows_for_every_default_category(category: str) -> None:
    content = (GROCERIES / f"{category}.html").read_bytes()
    page = parse_page(content, f"https://www.kupi.cz/slevy/{category}", FETCHED_AT)
    assert page.items and page.next_url is None
    for item in page.items:
        assert item.error is None
        offer = Offer.model_validate(item.candidate)
        assert offer.product.category == category.replace("-", " ")
        assert item.evidence.metadata["category_slug"] == category
        assert item.evidence.content == content


def test_default_coverage_and_single_category_override() -> None:
    default = KupiSettings(_env_file=None)
    assert len(default.listing_urls) == 13
    assert {"maso-drubez-a-ryby", "ovoce-a-zelenina", "mlecne-vyrobky-a-vejce", "pro-deti"} <= set(
        default.category_slugs
    )
    assert KupiSettings(listing_url=LISTING_URL).listing_urls == (LISTING_URL,)


@pytest.mark.parametrize("slugs", [(), ("maso", "maso"), ("../private",), ("maso?ord=price",)])
def test_invalid_category_lists(slugs: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        KupiSettings(category_slugs=slugs)


@pytest.mark.parametrize(
    "text,amount,unit",
    [
        ("/ 6x 0.5 l", "3", "l"),
        ("/ 2 × 500 ml", "1000", "ml"),
        ("/ 3x 80 g", "240", "g"),
        ("/ 16 dávek", "16", "serving"),
        ("/ 4x 80 ks", "320", "piece"),
    ],
)
def test_multipack_and_serving_quantities(text: str, amount: str, unit: str) -> None:
    quantity = parse_quantity(text)
    assert quantity.amount == Decimal(amount) and quantity.unit == unit


def test_one_day_and_tomorrow_validity() -> None:
    from datetime import date

    assert parse_validity("ve st 30. 9.", FETCHED_AT.date()) == (
        date(2026, 9, 30),
        date(2026, 9, 30),
    )
    assert parse_validity("v pá 2. 10.", FETCHED_AT.date()) == (
        date(2026, 10, 2),
        date(2026, 10, 2),
    )
    assert parse_validity("zítra končí", FETCHED_AT.date()) == (None, date(2026, 10, 1))


def test_packaging_and_opaque_package_prices() -> None:
    alcohol = parse_page(
        (GROCERIES / "alkohol.html").read_bytes(), "https://www.kupi.cz/slevy/alkohol", FETCHED_AT
    )
    six_pack = next(
        Offer.model_validate(i.candidate)
        for i in alcohol.items
        if "6x 0.5 l" in i.evidence.metadata["quantity_text"]
    )
    assert six_pack.price_basis.unit == "package" and six_pack.price_basis.amount == 1
    assert six_pack.product.quantity.amount == 3 and six_pack.product.quantity.unit == "l"
    assert six_pack.product.sku.endswith(":6x")
    assert six_pack.unit_price.amount == (six_pack.current_price / 3).quantize(Decimal("0.0001"))
    food = parse_page(
        (GROCERIES / "lahudky.html").read_bytes(), "https://www.kupi.cz/slevy/lahudky", FETCHED_AT
    )
    lower_bound = next(
        Offer.model_validate(i.candidate)
        for i in food.items
        if "cena od" in i.candidate["promotion"]["conditions"]
    )
    assert (
        lower_bound.price_qualifier == "from" and lower_bound.unit_price.price_qualifier == "from"
    )
    assert lower_bound.product.quantity is None and lower_bound.price_basis.unit == "package"
    assert lower_bound.product.sku.endswith(":unspecified")


def test_meat_quotes_do_not_invent_package_weight() -> None:
    page = parse_page(
        (GROCERIES / "maso-drubez-a-ryby.html").read_bytes(),
        "https://www.kupi.cz/slevy/maso-drubez-a-ryby",
        FETCHED_AT,
    )
    offers = [Offer.model_validate(item.candidate) for item in page.items]
    packed = next(o for o in offers if "baleno" in o.promotion.conditions)
    assert packed.product.quantity is None and packed.price_basis.unit == "kg"
    counter_sale = next(o for o in offers if "pultový prodej" in o.promotion.conditions)
    assert counter_sale.product.variable_weight and counter_sale.product.quantity is None
    assert counter_sale.price_basis.unit in {"g", "kg"}


async def test_default_multicategory_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("grocery_agent.stores.kupi.adapter.utc_now", lambda: FETCHED_AT)
    keys = set()
    for category in DEFAULT_CATEGORIES:
        page = parse_page(
            (GROCERIES / f"{category}.html").read_bytes(),
            f"https://www.kupi.cz/slevy/{category}",
            FETCHED_AT,
        )
        keys.update(i.candidate["offer_key"] for i in page.items)
    async with httpx.AsyncClient(transport=httpx.MockTransport(grocery_response)) as client:
        await assert_adapter_contract(
            lambda: KupiAdapter(KupiSettings(request_delay_seconds=0, _env_file=None)),
            AdapterContext(client),
            expected_count=len(keys),
        )


async def test_repeated_default_grocery_scrape_does_not_add_history(
    repository,
    snapshots,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("grocery_agent.stores.kupi.adapter.utc_now", lambda: FETCHED_AT)
    source = KupiAdapter(KupiSettings(request_delay_seconds=0, _env_file=None))
    pipeline = ScrapePipeline(repository, snapshots, ExactGTINResolver())
    async with httpx.AsyncClient(transport=httpx.MockTransport(grocery_response)) as client:
        first = await pipeline.run(source, AdapterContext(client))
        second = await pipeline.run(source, AdapterContext(client))
    assert first.status == second.status == "success"
    assert first.accepted > 100 and first.changed == first.accepted
    assert second.accepted == first.accepted and second.changed == 0


async def test_category_failure_continues_and_reports_incomplete_coverage(
    repository,
    snapshots,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr("grocery_agent.stores.kupi.adapter.utc_now", lambda: FETCHED_AT)
    caplog.set_level(logging.INFO)
    categories = ("ovoce-a-zelenina", "maso-drubez-a-ryby")
    requests = []

    def transport(request):
        requests.append(request.url.path)
        if request.url.path.endswith("ovoce-a-zelenina"):
            return httpx.Response(200, text="broken category")
        return grocery_response(request)

    pipeline = ScrapePipeline(repository, snapshots, ExactGTINResolver())
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result = await pipeline.run(
            adapter(listing_url=None, category_slugs=categories), AdapterContext(client)
        )
    assert result.status == "failed" and result.errors == 1 and result.rejected == 1
    assert result.accepted > 0
    assert "/slevy/maso-drubez-a-ryby" in requests
    summaries = [r.fields for r in caplog.records if r.getMessage() == "category_finished"]
    assert len(summaries) == 2 and {s["run_id"] for s in summaries} == {result.run_id}
    assert summaries[0]["status"] == "failed" and summaries[1]["status"] == "success"


async def test_duplicate_crosscategory_page_still_reaches_new_offers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("grocery_agent.stores.kupi.adapter.utc_now", lambda: FETCHED_AT)
    root1 = "/slevy/ovoce-a-zelenina"
    root2 = "/slevy/maso-drubez-a-ryby"
    first = BeautifulSoup((FIXTURES / "page1.html").read_bytes(), "lxml")
    for link in first.select("a.load_discounts"):
        link.decompose()

    def transport(request):
        if request.url.path == "/robots.txt":
            return fixture_response(request)
        if request.url.path == root1:
            return httpx.Response(200, text=str(first))
        assert request.url.path == root2
        file = "page2.html" if request.url.params.get("page") else "page1.html"
        return httpx.Response(200, text=(FIXTURES / file).read_text().replace(root1, root2))

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        items = [
            i
            async for i in adapter(
                listing_url=None, category_slugs=("ovoce-a-zelenina", "maso-drubez-a-ryby")
            ).fetch_offers(AdapterContext(client))
        ]
    assert len(items) == 90 and all(i.error is None for i in items)
