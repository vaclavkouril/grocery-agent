"""Pure parsers for the public catalog context and product-card responses."""

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from grocery_agent.models.product import Quantity, Unit
from grocery_agent.stores.base import AcquisitionItem, SourceEvidence

BASE_URL = "https://www.rohlik.cz"


def load_json(content: bytes) -> Any:
    return json.loads(content, parse_float=Decimal)


@dataclass(frozen=True)
class CatalogContext:
    warehouse_id: int
    locality: str
    categories: dict[int, str]

    @property
    def scope(self) -> str:
        text = unicodedata.normalize("NFKD", self.locality).encode("ascii", "ignore").decode()
        slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
        if not slug:
            raise ValueError("missing catalog locality")
        return f"rohlik:online:warehouse:{self.warehouse_id}:locality:{slug}"


def parse_context(content: bytes) -> CatalogContext:
    try:
        node = BeautifulSoup(content, "lxml").find("script", id="__NEXT_DATA__")
        if node is None:
            raise ValueError("missing public catalog state")
        data = load_json(node.get_text().encode())
        queries = data["props"]["pageProps"]["dehydratedState"]["queries"]
        values = {q["queryKey"][0]: q["state"]["data"] for q in queries}
        user = values["userGtmData"]
        if user["type"] != "Anonymous":
            raise ValueError("only anonymous public catalogs are supported")
        warehouse = user["warehouse_id"]
        locality = values["first-delivery"]["data"]["deliveryLocationText"]
        if type(warehouse) is not int or warehouse <= 0 or not isinstance(locality, str):
            raise ValueError("invalid warehouse/locality")
        categories = {item["id"]: item["name"] for item in values["navigationCategories"]["items"]}
        context = CatalogContext(warehouse, locality, categories)
        _ = context.scope
        return context
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("invalid public catalog context") from exc


def parse_ids(content: bytes, category_id: int) -> list[int]:
    data = load_json(content)
    if (
        not isinstance(data, dict)
        or data.get("categoryId") != category_id
        or data.get("categoryType") != "normal"
        or not isinstance(data.get("productIds"), list)
    ):
        raise ValueError("invalid category product page")
    ids = data["productIds"]
    if any(type(sku) is not int or sku <= 0 for sku in ids) or len(ids) != len(set(ids)):
        raise ValueError("invalid or duplicate product IDs")
    return [int(sku) for sku in ids]


def parse_count(content: bytes) -> int:
    data = load_json(content)
    count = data.get("results") if isinstance(data, dict) else None
    if type(count) is not int or count < 0:
        raise ValueError("invalid category product count")
    return count


def _amount(text: str) -> Quantity | None:
    match = re.fullmatch(
        r"\s*(?:(\d+)\s*[x×]\s*)?(\d+(?:[.,]\d+)?)\s*"
        r"(kg|g|ml|l|ks|balení)\s*",
        text,
        re.IGNORECASE,
    )
    if match is None:
        return None
    unit = {"ks": Unit.PIECE, "balení": Unit.PACKAGE}.get(match[3].lower())
    return Quantity(
        amount=Decimal(match[1] or "1") * Decimal(match[2].replace(",", ".")),
        unit=unit or Unit(match[3].lower()),
    )


def _price(value: Any) -> Decimal:
    if isinstance(value, (float, bool)) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("missing or invalid price")
    result = Decimal(value)
    if not result.is_finite() or result < 0:
        raise ValueError("invalid price")
    return result


def _offers(card: dict[str, Any], catalog: CatalogContext, category: str) -> list[dict[str, Any]]:
    if card.get("type") != "PRODUCT":
        raise ValueError("unsupported product card type")
    sku = card["productId"]
    if type(sku) is not int or sku <= 0:
        raise ValueError("invalid SKU")
    if not isinstance(card["name"], str) or not card["name"].strip():
        raise ValueError("missing product name")
    weighted = card["weightedItem"]
    if type(weighted) is not bool:
        raise ValueError("invalid weightedItem flag")
    quantity = None if weighted else _amount(card["textualAmount"])
    prices = card["prices"]
    if prices["currency"] != "CZK":
        raise ValueError("expected CZK prices")
    original = _price(prices["originalPrice"]) if prices["originalPrice"] is not None else None
    sale = _price(prices["salePrice"]) if prices["salePrice"] is not None else None
    if original is None and sale is None:
        raise ValueError("missing item price")
    if sale is not None and original is not None and sale >= original:
        raise ValueError("sale price is not lower than original price")
    if weighted:
        if card["unit"] != "kg":
            raise ValueError("unsupported variable-weight price unit")
        current = _price(prices["unitPrice"])
        basis = {"amount": 1, "unit": "kg"}
    else:
        current = sale if sale is not None else _price(original)
        basis = {"amount": 1, "unit": "package"}
    stock = card["stock"]["availabilityStatus"]
    availability = {
        "AVAILABLE": "available",
        "UNAVAILABLE": "unavailable",
        "SOLD_OUT": "unavailable",
    }.get(stock, "unknown")
    offer: dict[str, Any] = {
        "product": {
            "store_id": "rohlik",
            "sku": str(sku),
            "name": card["name"],
            "brand": card.get("brand"),
            "category": category,
            "quantity": quantity.model_dump(exclude_computed_fields=True) if quantity else None,
            "variable_weight": weighted,
        },
        "offer_key": "standard",
        "scope": catalog.scope,
        "current_price": current,
        "price_basis": basis,
        "currency": "CZK",
        "availability": availability,
        "source_url": f"{BASE_URL}/{sku}",
    }
    if sale is None:
        return [offer]
    label = prices.get("saleText") or "Rohlík sale"
    if not isinstance(label, str):
        raise ValueError("invalid sale label")
    # The anonymous catalog displays Xtra/personal prices alongside ordinary sale prices.
    # Treat ambiguous 'for you' discounts conservatively as membership-dependent.
    conditional = bool(re.search(r"pro vás|xtra|premium|klub", label, re.IGNORECASE))
    percentage_only = re.fullmatch(r"-?\s*\d+(?:[.,]\d+)?\s*%", label.strip())
    if not conditional and not percentage_only and label != "Rohlík sale":
        raise ValueError("unrecognized sale conditions")
    promotion: dict[str, Any] = {
        "kind": "loyalty"
        if conditional
        else ("advertised" if weighted or original is None else "price_cut"),
        "label": label,
        "requires_loyalty": conditional,
        "discount_reference": "unspecified" if weighted or original is None else "regular_price",
    }
    claimed_discount = re.search(r"(\d+(?:[.,]\d+)?)\s*%", label)
    if claimed_discount:
        promotion["advertised_discount_percent"] = Decimal(claimed_discount[1].replace(",", "."))
    if conditional:
        promotion["conditions"] = "Displayed Xtra/personal discount; confirm eligibility at Rohlík"
    if not weighted and original is not None:
        offer["regular_price"] = original
    offer["promotion"] = promotion
    if conditional:
        offer["offer_key"] = "loyalty"
    expiry = prices.get("saleValidTill")
    if expiry is not None:
        end = datetime.fromisoformat(expiry)
        if end.utcoffset() is None:
            raise ValueError("sale expiry lacks timezone")
        local = end.astimezone(ZoneInfo("Europe/Prague"))
        # Dates are inclusive. Do not extend a morning expiry to the end of that day.
        offer["valid_until"] = (
            local.date()
            if (local.hour, local.minute) >= (23, 59)
            else local.date() - timedelta(days=1)
        )
    if conditional and not weighted and original is not None:
        standard = {
            k: v
            for k, v in offer.items()
            if k
            not in {
                "regular_price",
                "promotion",
                "valid_until",
            }
        }
        standard.update(offer_key="standard", current_price=original)
        return [standard, offer]
    return [offer]


def parse_cards(
    content: bytes,
    url: str,
    fetched_at: datetime,
    catalog: CatalogContext,
    category: str,
) -> list[AcquisitionItem]:
    cards = load_json(content)
    if not isinstance(cards, list):
        raise ValueError("product cards must be a JSON array")
    items = []
    for index, card in enumerate(cards):
        evidence = SourceEvidence(
            content=content,
            url=url,
            media_type="application/json",
            fetched_at=fetched_at,
            locator=f"$[{index}]",
            metadata={
                "source_id": "rohlik",
                "parser_version": 1,
                "warehouse_id": catalog.warehouse_id,
                "locality": catalog.locality,
                "category": category,
                "quantity_policy": "fixed explicit contents only; approximate weights omitted",
            },
        )
        try:
            if not isinstance(card, dict):
                raise ValueError("product card is not an object")
            offers = _offers(card, catalog, category)
        except (ValueError, KeyError, TypeError, AttributeError, ArithmeticError) as exc:
            items.append(AcquisitionItem(evidence=evidence, error=f"product card failure: {exc}"))
            continue
        items.extend(AcquisitionItem(evidence=evidence, candidate=offer) for offer in offers)
    return items
