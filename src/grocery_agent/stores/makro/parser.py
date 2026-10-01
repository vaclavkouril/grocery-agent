"""Parse rendered assortment cards. No account/session state is retained as evidence."""

import copy
import re
import unicodedata
from datetime import datetime
from decimal import Decimal
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from grocery_agent.stores.base import AcquisitionItem, SourceEvidence

BASE_URL = "https://sortiment.makro.cz"
MONEY = r"\d+(?:[\s\u00a0]\d{3})*(?:[.,]\d{1,4})?"
PRODUCT_PATH = r"/shop/pv/(BTY-[A-Z]\d+)/(\d{4})/(\d{4})/[^/?#]+"


def text(node: Tag | None) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def decimal(value: str) -> Decimal:
    return Decimal(re.sub(r"\s", "", value).replace(",", "."))


def scope(store: str, account: str) -> str:
    slug = unicodedata.normalize("NFKD", store).encode("ascii", "ignore").decode().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
    if not slug or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", account) is None:
        raise ValueError("invalid store/customer scope")
    return f"makro:in-store:branch:{slug}:account:{account}"


def category_url(path: str) -> str:
    return f"{BASE_URL}/shop/category/{quote(path, safe='/')}"


def identity(card: Tag) -> tuple[str, str]:
    anchor = card.select_one("a.title[href]")
    if anchor is None:
        raise ValueError("missing Makro product link")
    url = urlsplit(urljoin(BASE_URL, str(anchor["href"])))
    match = re.fullmatch(PRODUCT_PATH, unquote(url.path))
    if (
        url.scheme != "https"
        or url.netloc != "sortiment.makro.cz"
        or url.query
        or url.fragment
        or not match
    ):
        raise ValueError("invalid Makro product identity")
    return ":".join(match.groups()), url.geturl()


def coverage(content: bytes) -> tuple[int, int]:
    soup = BeautifulSoup(content, "lxml")
    # Only ordinary category result counts count towards completeness.
    counts = [
        re.fullmatch(r"Zobrazeno (\d+) z (\d+) výsledků", text(p))
        for p in soup.select("p.text-default")
    ]
    matches = [m for m in counts if m]
    if len(matches) != 1:
        raise ValueError("missing or ambiguous Makro catalog coverage")
    seen, total = int(matches[0][1]), int(matches[0][2])
    if not 0 < seen <= total:
        raise ValueError("invalid Makro catalog coverage")
    return seen, total


def snapshot(content: bytes) -> bytes:
    soup = BeautifulSoup(content, "lxml")
    store = soup.select_one(".brandbar-store-data-name")
    cards = soup.select(".sd-articlecard")
    if store is None or not cards:
        raise ValueError("missing Makro branch or rendered product cards")
    coverage(content)
    output = BeautifulSoup("<html><body></body></html>", "lxml")
    assert output.body is not None
    nodes = [store, *soup.select("h1, p.text-default")]
    for node in nodes:
        output.body.append(copy.copy(node))
    for card in cards:
        public_card = output.new_tag("div", attrs={"class": "sd-articlecard"})
        for selector in (
            "a.title",
            ".sd-shared--bundle-selector",
            ".sd-shared--promotion-labels",
            ".availability-details-inner",
            ".bottom-part",
            ".sd-add-to-basket-control",
        ):
            part = card.select_one(selector)
            if part:
                public_card.append(copy.copy(part))
        output.body.append(public_card)
    # Retain product, package and price markup only, not cart/customer headers,
    # application JSON, tokens, customer IDs, images or tracking attributes.
    for node in output.find_all(["script", "svg", "img", "input"]):
        node.decompose()
    for node in output.find_all(True):
        node.attrs = {k: v for k, v in node.attrs.items() if k in {"class", "href"}}
        if node.has_attr("href"):
            href = str(node["href"])
            parsed = urlsplit(urljoin(BASE_URL, href))
            if (
                parsed.scheme != "https"
                or parsed.netloc != "sortiment.makro.cz"
                or not re.fullmatch(PRODUCT_PATH, unquote(parsed.path))
                or parsed.query
                or parsed.fragment
            ):
                del node["href"]
    return str(output).encode()


def _contents(name: str, packaging: str) -> tuple[dict[str, Any] | None, bool]:
    if re.search(r"\bcca\b|\bváž\b|přibližně", f"{name} {packaging}", re.I):
        return None, True
    pack_match = re.fullmatch(
        r"(?:Balení po )?(\d+)(?:\s+(?:kus|kusy|kusů|balení|sáček|láhev))?", packaging
    )
    if not pack_match or int(pack_match[1]) < 1:
        raise ValueError("unknown mandatory packaging quantity")
    pack_count = int(pack_match[1])
    matches = list(
        re.finditer(
            r"(?<![\d.,])(?:(\d+)\s*[x×]\s*)?(\d+(?:[.,]\d+)?)\s*(kg|g|ml|l|ks)\b", name, re.I
        )
    )
    if not matches:
        return ({"amount": str(pack_count), "unit": "piece"} if pack_count > 1 else None), False
    match = matches[-1]
    title_count = int(match[1] or "1")
    nested = re.search(
        r"(\d+)\s*[x×]\s*\(\s*(\d+)\s*[x×]\s*(\d+(?:[.,]\d+)?)\s*(kg|g|ml|l)\s*\)",
        name,
        re.I,
    )
    if nested:
        title_count = int(nested[1]) * int(nested[2])
    if title_count > 1 and pack_count not in {1, title_count}:
        raise ValueError("title multipack and selected packaging disagree")
    amount = decimal(match[2]) * max(title_count, pack_count)
    return {
        "amount": str(amount),
        "unit": "piece" if match[3].lower() == "ks" else match[3].lower(),
    }, False


def _candidate(card: Tag, offer_scope: str, category: str) -> dict[str, Any]:
    sku, url = identity(card)
    name = text(card.select_one("a.title"))
    packaging = text(card.select_one(".bundle.packaging-type"))
    quantity, weighted = _contents(name, packaging)
    price_display = card.select_one(".price-display")
    if price_display is None:
        raise ValueError("Makro prices unavailable: sign in with an eligible customer account")
    rows = price_display.select(".primary, .secondary")
    gross = []
    for row in rows:
        match = re.fullmatch(rf"vč\.\s*DPH\s*({MONEY})\s*Kč(?:\s*/\s*(kg|g))?", text(row))
        if match:
            gross.append((decimal(match[1]), match[2]))
    if len(gross) != 1:
        raise ValueError("missing or ambiguous VAT-inclusive selling price")
    amount, quoted_unit = gross[0]
    # Makro's unitGross is the selected bundle's gross amount, not pieceGross.
    # For weighed articles the separate Cena / kg hint supplies the price basis.
    per_kg = "Cena / kg" in text(price_display.select_one(".additional-price-info-row"))
    if weighted:
        if not per_kg and quoted_unit not in {"kg", "g"}:
            raise ValueError("weighed product has no explicit mass price basis")
        basis = {"amount": "1", "unit": quoted_unit or "kg"}
        minimum = increment = None
    else:
        if per_kg or quoted_unit:
            raise ValueError("fixed-pack card unexpectedly quotes a mass price")
        basis = {"amount": "1", "unit": "package"}
        minimum = increment = basis
    # Volume discounts require their actual tier thresholds. A lowest/from quote
    # is never treated as the price of a minimum pack.
    if card.select(".volume-discount, .dnr-more-promo-label") or re.search(
        r"Klesající cena|nejnižší cena|\bod\s+\d|\d+\s*za\s*\d", text(card), re.I
    ):
        raise ValueError("conditional/volume price requires verified purchase thresholds")
    states = card.select_one(".availability-details-inner .state")
    classes: list[str] = states.get_attribute_list("class") if states else []
    availability = (
        "available"
        if "AVAILABLE" in classes
        else "unavailable"
        if "NOT_AVAILABLE" in classes or "UNAVAILABLE" in classes
        else "unknown"
    )
    charges = text(card.select_one(".bottom-part"))
    deposit_match = re.search(
        rf"(?:záloha|vratný obal)\s*vč\.\s*DPH\s*({MONEY})\s*Kč", charges, re.I
    )
    fee_match = re.search(rf"poplatek\s*vč\.\s*DPH\s*({MONEY})\s*Kč", charges, re.I)
    for charge in (deposit_match, fee_match):
        if charge and re.match(r"\s*/", charges[charge.end() :]):
            raise ValueError("item charge has an unverified per-unit basis")
    # Absence of a rendered deposit is not proof that there is no deposit.
    deposit = (
        decimal(deposit_match[1])
        if deposit_match
        else Decimal(0)
        if "Bez zálohy" in charges
        else None
    )
    fee = (
        decimal(fee_match[1])
        if fee_match
        else Decimal(0)
        if "Bez dalších poplatků" in charges
        else None
    )
    return {
        "product": {
            "store_id": "makro",
            "sku": sku,
            "name": name,
            "quantity": quantity,
            "variable_weight": weighted,
            "category": category,
        },
        "scope": offer_scope,
        "current_price": amount,
        "price_basis": basis,
        "availability": availability,
        "source_url": url,
        "purchase_terms": {
            "vat_included": True,
            "minimum_quantity": minimum,
            "quantity_increment": increment,
            "deposit_per_basis": deposit,
            "mandatory_fee_per_basis": fee,
            "membership_required": True,
            "conditions": (
                f"Selected packaging: {packaging}. Customer-specific in-store price; "
                "basket charges not included."
            ),
        },
    }


def parse_cards(
    content: bytes,
    url: str,
    fetched_at: datetime,
    account: str,
    category: str,
    expected_store: str | None = None,
) -> list[AcquisitionItem]:
    soup = BeautifulSoup(content, "lxml")
    store = text(soup.select_one(".brandbar-store-data-name"))
    if not store or (expected_store and store != expected_store):
        raise ValueError("selected Makro branch differs from configured store_name")
    offer_scope = scope(store, account)
    cards = soup.select(".sd-articlecard")
    if not cards:
        raise ValueError("missing Makro product cards")
    result = []
    for index, card in enumerate(cards):
        evidence = SourceEvidence(
            content,
            url,
            "text/html",
            fetched_at,
            f".sd-articlecard:nth-of-type({index + 1})",
            {
                "source_id": "makro",
                "scope": offer_scope,
                "snapshot_policy": "product markup only; account/session state removed",
            },
        )
        try:
            result.append(
                AcquisitionItem(evidence, candidate=_candidate(card, offer_scope, category))
            )
        except ValueError as exc:
            result.append(AcquisitionItem(evidence, error=str(exc)))
    return result
