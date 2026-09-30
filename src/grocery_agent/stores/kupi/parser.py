import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, Tag

from grocery_agent.models.normalization import canonical_quantity
from grocery_agent.models.product import Quantity, Unit
from grocery_agent.stores.base import AcquisitionItem, SourceEvidence
from grocery_agent.stores.kupi.normalization import (
    clean_text,
    czech_decimal,
    parse_quantity,
    parse_validity,
    slug,
)


@dataclass(frozen=True)
class ParsedPage:
    items: tuple[AcquisitionItem, ...]
    next_url: str | None
    locality: str


def text(tag: Tag, selector: str, *, required: bool = True) -> str:
    found = tag.select_one(selector)
    value = clean_text(found.get_text(" ", strip=True)) if found else ""
    if required and not value:
        raise ValueError(f"missing required element: {selector}")
    return value


def attribute(tag: Tag, key: str) -> str:
    value = tag.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing attribute: {key}")
    return value


def parse_row(row: Tag, page_url: str, locality: str, fetched_at: datetime) -> dict[str, Any]:
    group = row.find_parent(class_="group_discounts")
    if not isinstance(group, Tag):
        raise ValueError("offer has no product group")
    product_id = attribute(row, "data-product")
    discount_id = attribute(row, "data-discount")
    if not product_id.isdigit() or not discount_id.isdigit():
        raise ValueError("invalid Kupi product/campaign identity")
    product_link = group.select_one(".product_name h2 a")
    retailer_link = row.select_one('.discounts_shop_name a[href^="/letaky/"]')
    if not product_link or not retailer_link:
        raise ValueError("missing product or retailer identity")
    retailer = attribute(retailer_link, "href").removeprefix("/letaky/").strip("/")
    retailer = {"penny-market": "penny", "kosik-cz": "kosik"}.get(retailer, retailer)
    # Source-specific groups are comparison categories, not verified retailer SKUs/GTINs.
    quantity_text = text(row, ".discount_amount", required=False)
    basis = (
        parse_quantity(quantity_text) if quantity_text else Quantity(amount=1, unit=Unit.PACKAGE)
    )
    normalized_amount, normalized_unit = canonical_quantity(basis.amount, basis.unit)
    size_key = (
        f"{normalized_amount.normalize():f}{normalized_unit}" if quantity_text else "unspecified"
    )
    multipack = re.search(r"(\d+)\s*[x×]", quantity_text)
    if multipack:
        size_key += f":{multipack[1]}x"
    sku = f"kupi:{product_id}:{size_key}"
    note = text(row, ".discount_note", required=False)
    # 'Packed' does not establish actual package weight for meat sold per kg/100 g.
    contents = basis if multipack else None
    explicit_contents = re.search(
        r"(?:vanička|balení|sáček|láhev|plechovka)\s*:?\s*(\d+(?:[.,]\d+)?\s*(?:kg|g|ml|l|ks))\b",
        note,
        re.I,
    )
    if explicit_contents:
        contents = parse_quantity(explicit_contents[1])
    product: dict[str, Any] = {
        "store_id": retailer,
        "sku": sku,
        "name": clean_text(product_link.get_text(" ", strip=True)),
        "category": urlsplit(page_url).path.removeprefix("/slevy/").replace("-", " "),
        "quantity": contents.model_dump(mode="json") if contents else None,
        "variable_weight": basis.unit in {Unit.KG, Unit.G}
        and bool(re.search(r"\bvážen[éýá]\b|na váhu|pultový prodej", note, re.I)),
    }
    # For a known pack, price is for one pack with the advertised contents.
    # Otherwise retain the explicitly advertised mass/volume/piece price basis.
    price_basis = {"amount": "1", "unit": "package"} if multipack else basis.model_dump(mode="json")
    if product["variable_weight"]:
        product["quantity"] = None
        price_basis = basis.model_dump(mode="json")
    loyalty = text(row, ".discounts_club", required=False)
    percent_text = text(row, ".discount_percentage", required=False)
    percent = None
    if percent_text:
        match = re.fullmatch(r"[−–-]?\s*(\d+(?:[.,]\d+)?)\s*%", percent_text)
        if not match:
            raise ValueError("unrecognized advertised percentage")
        percent = str(czech_decimal(match[1]))
    start, end = parse_validity(
        text(row, ".discounts_validity"), fetched_at.astimezone(ZoneInfo("Europe/Prague")).date()
    )
    retailer_label = text(row, ".discounts_shop_name")
    terms = "; ".join(part for part in (retailer_label, note, loyalty) if part)
    product_url = urljoin(page_url, attribute(product_link, "href"))
    if not product_url.startswith("https://www.kupi.cz/sleva/"):
        raise ValueError("unexpected product URL")
    return {
        "product": product,
        "offer_key": f"kupi:{discount_id}",
        "scope": f"kupi:locality:{slug(locality)}",
        "current_price": str(czech_decimal(text(row, ".discount_price_value"))),
        "price_qualifier": "from" if re.search(r"\bcena\s+od\b", note, re.I) else "exact",
        "regular_price": None,
        "price_basis": price_basis,
        "currency": "CZK",
        "promotion": {
            "kind": "loyalty" if loyalty else "advertised",
            "label": loyalty or "Akční nabídka",
            "requires_loyalty": bool(loyalty),
            "conditions": terms,
            "advertised_discount_percent": percent,
            "discount_reference": "unspecified",
        },
        "valid_from": start.isoformat() if start else None,
        "valid_until": end.isoformat() if end else None,
        "availability": "unknown",
        "source_url": product_url,
    }


def parse_page(content: bytes, url: str, fetched_at: datetime) -> ParsedPage:
    """Pure parser: one bounded HTML page, item failures retained independently."""
    if fetched_at.utcoffset() is None:
        raise ValueError("fetch time must be timezone-aware")
    soup = BeautifulSoup(content, "lxml")
    locality = text(soup, ".locality_near_headline a[data-user-localizator]")
    rows = soup.select(".discount_row")
    if not rows:
        raise ValueError("no offer rows: listing is empty or page structure changed")
    items = []
    for index, row in enumerate(rows):
        discount_id = str(row.get("data-discount", ""))
        evidence = SourceEvidence(
            content=content,
            url=url,
            media_type="text/html",
            fetched_at=fetched_at,
            locator=f"css=.discount_row; index={index}; data-discount={discount_id}",
            metadata={
                "source_id": "kupi",
                "locality": locality,
                "retailer_label": text(row, ".discounts_shop_name", required=False),
                "retailer_source_id": str(row.get("data-shop", "")),
                "product_source_id": str(row.get("data-product", "")),
                "discount_source_id": discount_id,
                "validity_text": text(row, ".discounts_validity", required=False),
                "year_policy": "nearest year within 183 days; Europe/Prague",
                "category_slug": urlsplit(url).path.removeprefix("/slevy/"),
                "quantity_text": text(row, ".discount_amount", required=False),
                "price_basis_policy": "explicit quote"
                if text(row, ".discount_amount", required=False)
                else "one advertised package; contents unknown",
            },
        )
        try:
            candidate = parse_row(row, url, locality, fetched_at)
            items.append(AcquisitionItem(evidence=evidence, candidate=candidate))
        except ValueError as exc:
            items.append(AcquisitionItem(evidence=evidence, error=str(exc)))
    next_links = [
        link
        for link in soup.select("a.load_discounts[href]")
        if "další" in clean_text(link.get_text()).lower()
    ]
    if len(next_links) > 1:
        raise ValueError("ambiguous next page")
    next_url = urljoin(url, attribute(next_links[0], "href")) if next_links else None
    return ParsedPage(tuple(items), next_url, locality)
