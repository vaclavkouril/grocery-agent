import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from bs4 import BeautifulSoup
from pydantic import ValidationError
from sqlalchemy import Engine

from grocery_agent.matching.base import ExactGTINResolver
from grocery_agent.models.common import utc_now
from grocery_agent.models.offer import Offer
from grocery_agent.models.product import Quantity
from grocery_agent.persistence.reader import SQLAlchemyCurrentOfferReader
from grocery_agent.persistence.repository import SQLAlchemyOfferRepository, state_fingerprint
from grocery_agent.persistence.snapshots import FileSnapshotStore
from grocery_agent.pipeline.service import ScrapePipeline
from grocery_agent.stores.base import AdapterContext
from grocery_agent.stores.makro.adapter import CatalogDocument, MakroAdapter
from grocery_agent.stores.makro.config import MakroSettings
from grocery_agent.stores.makro.parser import coverage, parse_cards, snapshot
from grocery_agent.stores.robots import RobotsPolicy
from tests.makro_support import FIXTURES, URL, FixtureMakro, priced_source


def parse(content: bytes | None = None) -> list[Offer]:
    items = parse_cards(content or priced_source(), URL, utc_now(), "local", "potraviny")
    assert not any(item.error for item in items), [item.error for item in items]
    return [Offer.model_validate(item.candidate) for item in items]


def test_pack_cost_includes_vat_and_does_not_sell_one_inner_piece() -> None:
    rice = parse()[0]
    assert rice.current_price == 112
    assert rice.product.quantity == Quantity(amount="3000", unit="g")
    assert rice.price_basis == Quantity(amount="1", unit="package")
    assert rice.unit_price.amount == Decimal("37.3333")
    minimum = rice.minimum_purchase_cost
    assert minimum and minimum.total == 112 and minimum.merchandise_total == 112
    assert minimum.quantity == Quantity(amount="1", unit="package")
    assert rice.purchase_terms and rice.purchase_terms.membership_required
    assert ":account:local" in rice.scope and ":branch:makro-praha-stodulky:" in rice.scope
    assert rice.purchase_cost(Quantity(amount="1000", unit="g")) == minimum
    cost = rice.purchase_cost(Quantity(amount="4", unit="kg"))
    assert cost and cost.quantity.amount == 2 and cost.total == 224


def test_deposit_counts_towards_cash_due_even_when_refundable() -> None:
    drink = parse()[1]
    minimum = drink.minimum_purchase_cost
    assert drink.unit_price.amount == Decimal("24.2")
    assert minimum and minimum.deposit_total == 18 and minimum.total == Decimal("90.60")
    cost = drink.purchase_cost(Quantity(amount="4", unit="l"))
    assert cost and cost.deposit_total == 36 and cost.total == Decimal("181.20")


def test_unknown_charges_do_not_become_zero() -> None:
    eggs = parse()[2]
    cost = eggs.minimum_purchase_cost
    assert cost and cost.merchandise_total == 112 and cost.total is None
    assert cost.deposit_total is None and cost.mandatory_fee_total is None
    assert eggs.product.quantity == Quantity(amount="180", unit="piece")


def test_estimated_weight_does_not_manufacture_checkout_amount() -> None:
    beef = parse()[3]
    assert beef.product.variable_weight and beef.product.quantity is None
    assert beef.unit_price.amount == 112 and beef.unit_price.unit == "kg"
    assert beef.minimum_purchase_cost is None
    assert beef.purchase_cost(Quantity(amount="1", unit="kg")) is None


def test_nested_pack_contents_match_live_pack_selection_structure() -> None:
    soup = BeautifulSoup(priced_source(), "lxml")
    soup.select_one("a.title h4").string = "Test Drink 4 x (6 x 500 ml) plech"
    soup.select_one(".bundle.packaging-type").string = "Balení po 24"
    offer = parse(str(soup).encode())[0]
    assert offer.product.quantity == Quantity(amount="12000", unit="ml")
    assert offer.unit_price.amount == Decimal("9.3333")
    assert offer.minimum_purchase_cost.merchandise_total == 112


def test_canonical_sale_units_round_mass_up_to_increment() -> None:
    payload = parse()[3].model_dump(mode="json", exclude_computed_fields=True)
    payload["purchase_terms"].update(
        minimum_quantity={"amount": "500", "unit": "g"},
        quantity_increment={"amount": "100", "unit": "g"},
        deposit_per_basis="0",
        mandatory_fee_per_basis="0",
    )
    offer = Offer.model_validate(payload)
    assert offer.minimum_purchase_cost.total == 56
    cost = offer.purchase_cost(Quantity(amount="550", unit="g"))
    assert cost.quantity == Quantity(amount="0.6", unit="kg")
    assert cost.total == Decimal("67.20")
    other = json.loads(json.dumps(payload))
    other["purchase_terms"]["minimum_quantity"] = {"amount": "0.5", "unit": "kg"}
    other["purchase_terms"]["quantity_increment"] = {"amount": "0.1", "unit": "kg"}
    assert state_fingerprint(Offer.model_validate(other)) == state_fingerprint(offer)


def test_promotional_minimum_and_item_fee_count_in_purchase_cost() -> None:
    payload = parse()[0].model_dump(mode="json", exclude_computed_fields=True)
    payload["promotion"] = {"kind": "multibuy", "minimum_purchase": 3}
    payload["purchase_terms"]["mandatory_fee_per_basis"] = "2.50"
    offer = Offer.model_validate(payload)
    assert offer.minimum_purchase_cost.quantity.amount == 3
    assert offer.minimum_purchase_cost.total == Decimal("343.50")


@pytest.mark.parametrize(
    "patch",
    [
        {"vat_rate_percent": "101"},
        {"vat_rate_percent": 12.0},
        {"minimum_quantity": None},
        {"minimum_quantity": {"amount": "1", "unit": "kg"}},
        {"deposit_per_basis": "-1"},
    ],
)
def test_invalid_tax_or_sale_terms_are_rejected(patch: dict[str, Any]) -> None:
    payload = parse()[0].model_dump(mode="json", exclude_computed_fields=True)
    payload["purchase_terms"].update(patch)
    with pytest.raises(ValidationError):
        Offer.model_validate(payload)


def test_net_or_from_price_has_no_guaranteed_checkout_amount() -> None:
    payload = parse()[0].model_dump(mode="json", exclude_computed_fields=True)
    payload["purchase_terms"]["vat_included"] = False
    assert Offer.model_validate(payload).minimum_purchase_cost is None
    payload["purchase_terms"]["vat_included"] = True
    payload["price_qualifier"] = "from"
    assert Offer.model_validate(payload).minimum_purchase_cost is None


def test_unavailable_is_not_mistaken_for_available() -> None:
    soup = BeautifulSoup(priced_source(), "lxml")
    soup.select_one(".state")["class"] = ["state", "UNAVAILABLE"]
    assert parse(str(soup).encode())[0].availability == "unavailable"


def test_per_piece_charge_is_not_applied_once_per_case() -> None:
    soup = BeautifulSoup(priced_source(), "lxml")
    soup.select_one(".additional-price-info-row").string = "Záloha vč. DPH 3,00 Kč / kus"
    items = parse_cards(str(soup).encode(), URL, utc_now(), "local", "potraviny")
    assert items[0].error and "per-unit basis" in items[0].error


def test_live_anonymous_cards_have_no_prices() -> None:
    source = (FIXTURES / "anonymous.html").read_bytes()
    assert coverage(source) == (24, 17601)
    items = parse_cards(source, URL, utc_now(), "local", "potraviny")
    assert len(items) == 24
    assert all(i.candidate is None and i.error and "sign in" in i.error for i in items)


@pytest.mark.parametrize(
    "mutation,expected",
    [
        (lambda s: s.select_one(".secondary").decompose(), "VAT-inclusive"),
        (lambda s: setattr(s.select_one(".secondary"), "string", "od 112,00 Kč"), "VAT-inclusive"),
        (
            lambda s: s.select_one(".primary").attrs.update({"class": "primary volume-discount"}),
            "thresholds",
        ),
        (
            lambda s: setattr(s.select_one(".bundle.packaging-type"), "string", "Balení po 12"),
            "disagree",
        ),
        (
            lambda s: s.select_one("a.title").attrs.update(
                {"href": "https://other.test/shop/pv/BTY-X1/0032/0021/Rice"}
            ),
            "identity",
        ),
    ],
)
def test_ambiguous_prices_and_packs_are_rejected(mutation: Any, expected: str) -> None:
    soup = BeautifulSoup(priced_source(), "lxml")
    mutation(soup)
    items = parse_cards(str(soup).encode(), URL, utc_now(), "local", "potraviny")
    assert items[0].error and expected in items[0].error
    assert all(i.candidate for i in items[1:])


def test_wrong_branch_fails_before_offers_are_returned() -> None:
    with pytest.raises(ValueError, match="branch"):
        parse_cards(priced_source(), URL, utc_now(), "local", "potraviny", "makro Brno")


def test_evidence_strips_sessions_headers_and_tracking() -> None:
    soup = BeautifulSoup(priced_source(), "lxml")
    assert soup.body is not None
    header = BeautifulSoup(
        "<header>Customer secret address</header><script>SECRET_TOKEN</script>", "lxml"
    )
    soup.body.insert(0, header)
    title = soup.select_one("a.title")
    assert title is not None
    title["data-token"] = "SECRET_TOKEN"
    customer = soup.new_tag("div", attrs={"class": "customer-id"})
    customer.string = "PRIVATE_CUSTOMER_NUMBER"
    soup.select_one(".bundle-selector-customer-id-block").append(customer)
    clean = snapshot(str(soup).encode())
    assert b"SECRET_TOKEN" not in clean and b"secret address" not in clean
    assert b"data-token" not in clean and b"<script" not in clean
    assert b"PRIVATE_CUSTOMER_NUMBER" not in clean
    assert snapshot(clean) == clean


async def test_adapter_requires_login_without_successful_empty_run(tmp_path: Path) -> None:
    adapter = MakroAdapter(MakroSettings(_env_file=None, session_path=tmp_path / "missing"))
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError, match="saved login session"):
            _ = [i async for i in adapter.fetch_offers(AdapterContext(client))]


async def test_login_expiry_retains_rejections_then_fails_source() -> None:
    documents = [CatalogDocument((FIXTURES / "anonymous.html").read_bytes(), URL, "potraviny")]
    adapter = FixtureMakro(documents)
    async with httpx.AsyncClient() as client:
        stream = adapter.fetch_offers(AdapterContext(client))
        item = await anext(stream)
        assert item.error and item.evidence.content
        with pytest.raises(ValueError, match="session expired"):
            _ = [i async for i in stream]


@pytest.mark.parametrize("kind", ["partial", "count_mismatch", "repeated", "changed_total"])
async def test_incomplete_or_changing_catalog_cannot_succeed(kind: str) -> None:
    soup = BeautifulSoup(priced_source(), "lxml")
    count = soup.select_one("p.text-default span")
    assert count is not None
    count.string = (
        "Zobrazeno 4 z 8 výsledků" if kind != "count_mismatch" else "Zobrazeno 5 z 8 výsledků"
    )
    documents = [CatalogDocument(str(soup).encode(), URL, "potraviny")]
    if kind == "repeated":
        documents *= 2
    elif kind == "changed_total":
        count.string = "Zobrazeno 4 z 9 výsledků"
        documents.append(CatalogDocument(str(soup).encode(), URL, "potraviny"))
    async with httpx.AsyncClient() as client:
        with pytest.raises(ValueError):
            _ = [i async for i in FixtureMakro(documents).fetch_offers(AdapterContext(client))]


async def test_alternative_smaller_pack_is_separate_offer() -> None:
    soup = BeautifulSoup(priced_source(), "lxml")
    cards = soup.select(".sd-articlecard")
    for card in cards[1:]:
        card.decompose()
    card = cards[0]
    title = card.select_one("a.title")
    assert title is not None
    title["href"] = str(title["href"]).replace("/0021/", "/0022/")
    heading = title.select_one("h4")
    package = card.select_one(".bundle.packaging-type")
    price = card.select_one(".secondary")
    assert heading is not None and package is not None and price is not None
    heading.string, package.string, price.string = "Test Rice 500 g", "1 kus", "vč. DPH 22,40 Kč"
    documents = [
        CatalogDocument(priced_source(), URL, "potraviny"),
        CatalogDocument(str(soup).encode(), URL, "potraviny", False),
    ]
    async with httpx.AsyncClient() as client:
        items = [i async for i in FixtureMakro(documents).fetch_offers(AdapterContext(client))]
    offers = [Offer.model_validate(i.candidate) for i in items]
    assert len(offers) == 5 and offers[-1].minimum_purchase_cost.total == Decimal("22.40")
    assert offers[-1].product.sku != offers[0].product.sku


async def test_persistence_keeps_purchase_terms_and_computed_checkout_cost(
    engine: Engine, tmp_path: Path
) -> None:
    repository = SQLAlchemyOfferRepository(engine)
    pipeline = ScrapePipeline(
        repository, FileSnapshotStore(tmp_path / "snapshots"), ExactGTINResolver()
    )
    async with httpx.AsyncClient() as client:
        result = await pipeline.run(FixtureMakro(), AdapterContext(client))
    assert result.status == "success" and result.accepted == 4 and result.rejected == 0
    reader = SQLAlchemyCurrentOfferReader(engine)
    offers = [o.offer for o in reader.iter_offers(reader.latest_batch("makro"))]
    assert len(offers) == 4
    drink = next(o for o in offers if "Drink" in o.product.name)
    assert drink.minimum_purchase_cost.total == Decimal("90.60")


def test_purchase_metadata_does_not_change_historical_fingerprints(
    candidate: dict[str, Any],
) -> None:
    offer = Offer.model_validate(candidate)
    payload = offer.model_dump(
        mode="json", exclude={"source_url", "purchase_terms"}, exclude_computed_fields=True
    )
    original = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    assert state_fingerprint(offer) == original


def test_robots_respects_public_pages_and_restricted_endpoints() -> None:
    policy = RobotsPolicy.parse((FIXTURES / "robots.txt").read_text(), "grocery-agent")
    assert policy.permits(URL)
    assert not policy.permits("https://sortiment.makro.cz/backend/prices")
    assert not policy.permits("https://sortiment.makro.cz/shop/search?query=milk")


@pytest.mark.parametrize(
    "paths",
    [
        (),
        ("potraviny", "potraviny"),
        ("potraviny", "potraviny/mražené"),
        ("../login",),
        ("potraviny?x=1",),
    ],
)
def test_config_rejects_invalid_or_overlapping_categories(paths: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError):
        MakroSettings(_env_file=None, category_paths=paths)
