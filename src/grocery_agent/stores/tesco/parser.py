import copy
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from grocery_agent.models.normalization import canonical_quantity
from grocery_agent.models.product import Quantity, Unit
from grocery_agent.stores.base import AcquisitionItem, SourceEvidence

BASE_URL = "https://nakup.itesco.cz"
SCOPE = "tesco:online:anonymous"
BROWSE = "/shop/cs-CZ/browse/"
MONEY = r"\d+(?:[\s\u00a0]\d{3})*(?:[.,]\d+)?"


def _text(node: Tag | None) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def _decimal(value: str) -> Decimal:
    return Decimal(re.sub(r"\s", "", value).replace(",", "."))


def category_url(slug: str) -> str:
    if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) is None:
        raise ValueError("invalid category slug")
    return f"{BASE_URL}{BROWSE}{slug}/all"


def page_number(url: str, initial_url: str) -> int:
    parsed, initial = urlsplit(url), urlsplit(initial_url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "nakup.itesco.cz"
        or parsed.path != initial.path
        or set(query) - {"page", "count", "sortBy"}
        or any(len(values) != 1 for values in query.values())
        or query.get("sortBy", ["relevance"])[0] != "relevance"
        or query.get("count", ["24"])[0] not in {"24", "48"}
    ):
        raise ValueError("pagination escaped the public unfiltered category")
    page = query.get("page", ["1"])[0]
    if not page.isascii() or not page.isdigit() or int(page) < 1:
        raise ValueError("invalid page number")
    return int(page)


def public_snapshot(content: bytes) -> bytes:
    """Keep public product markup, navigation and coverage; remove session/tracking scripts."""
    soup = BeautifulSoup(content, "lxml")
    state = soup.find("script", type="application/discover+json")
    if state:
        data = json.loads(state.get_text())
        auth = data.get("mfe-plp", {}).get("props", {}).get("authentication", {})
        if auth.get("authenticated") is not False:
            raise ValueError("only the anonymous public Tesco catalog is supported")
    elif soup.find("meta", attrs={"name": "tesco-public-snapshot", "content": "1"}) is None:
        raise ValueError("missing anonymous public catalog context")
    grid = soup.select_one("#list-content")
    counts = soup.select('[data-testid="pagination-result-count"]')
    if grid is None or not counts:
        raise ValueError("missing product grid or coverage count")
    output = BeautifulSoup(
        '<html><head><meta name="tesco-public-snapshot" content="1"></head><body></body></html>',
        "lxml",
    )
    assert output.body is not None
    heading = soup.find("h1")
    if heading:
        output.body.append(copy.copy(heading))
    # Include exact public source elements needed for category discovery and pagination.
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"])
        path = urlsplit(urljoin(BASE_URL, href)).path
        if (
            path.startswith(BROWSE)
            and (
                re.fullmatch(BROWSE + r"[a-z0-9-]+/all", path)
                or anchor.get("aria-label") in {"Další", "Next"}
            )
            and href not in seen
        ):
            seen.add(href)
            output.body.append(copy.copy(anchor))
    output.body.append(copy.copy(counts[0]))
    output.body.append(copy.copy(grid))
    for node in output.find_all(["script", "svg", "img"]):
        node.decompose()
    return str(output).encode()


def discover_categories(content: bytes) -> dict[str, str]:
    soup = BeautifulSoup(content, "lxml")
    categories: dict[str, str] = {}
    for anchor in soup.find_all("a", href=True):
        url = urlsplit(urljoin(BASE_URL, str(anchor["href"])))
        match = re.fullmatch(BROWSE + r"([a-z0-9-]+)/all", url.path)
        if match and url.netloc == "nakup.itesco.cz" and not url.query:
            slug = match[1]
            if slug not in {"top-vyber", "novinky"} and _text(anchor):
                categories.setdefault(slug, _text(anchor))
    if not categories:
        raise ValueError("missing public department navigation")
    return categories


def _quoted_unit(value: str) -> tuple[Decimal, Quantity]:
    match = re.fullmatch(rf"({MONEY})\s*Kč/\s*(?:(\d+)\s*)?(kg|g|l|ml|kus|ks|balení)", value)
    if not match:
        raise ValueError("unsupported quoted unit price")
    aliases = {"kus": Unit.PIECE, "ks": Unit.PIECE, "balení": Unit.PACKAGE}
    unit = aliases.get(match[3]) or Unit(match[3])
    return _decimal(match[1]), Quantity(amount=match[2] or "1", unit=unit)


def _quantity(name: str) -> Quantity | None:
    if re.search(r"\bcca\b|přibližně|\bapprox", name, re.I):
        return None
    match = re.search(
        r"(?<![\d.,])(?:(\d+)\s*[x×]\s*)?"
        r"(\d+(?:[.,]\d+)?)\s*(kg|g|ml|l|ks|kusů|kusy)\s*$",
        name,
        re.I,
    )
    if not match:
        return None
    aliases = {"ks": Unit.PIECE, "kusů": Unit.PIECE, "kusy": Unit.PIECE}
    unit = aliases.get(match[3].lower()) or Unit(match[3].lower())
    return Quantity(amount=Decimal(match[1] or "1") * _decimal(match[2]), unit=unit)


def _offers(tile: Tag, category: str) -> list[dict[str, Any]]:
    sku = str(tile.get("data-testid", ""))
    if not sku.isascii() or not sku.isdigit():
        raise ValueError("missing retailer SKU")
    link = tile.select_one("h2 a")
    if link is None or str(link.get("href")) != f"{BASE_URL}/shop/cs-CZ/products/{sku}":
        raise ValueError("product link does not match retailer SKU")
    name = _text(link)
    if not name:
        raise ValueError("missing product name")
    price_text = _text(tile.select_one('[class*="product-tile-price__text"]'))
    match = re.fullmatch(rf"({MONEY})\s*Kč", price_text)
    if not match:
        raise ValueError("missing or ambiguous selling price")
    unit_text = _text(tile.select_one('[class*="product-tile-unit-price__subtext"]'))
    weighted = (
        tile.select_one(
            '[class*="LooseProduce-mode-selector"],'
            '[class*="catchweight-options"],'
            '[class*="variable-weight-product-message"]'
        )
        is not None
    )
    quantity = None if weighted else _quantity(name)
    current = _decimal(match[1])
    basis = Quantity(amount=1, unit=Unit.PACKAGE)
    if weighted:
        current, basis = _quoted_unit(unit_text)
        if basis.unit not in {Unit.KG, Unit.G}:
            raise ValueError("variable-weight products need a quoted mass price")
    elif quantity is None and unit_text:
        try:
            per_unit, unit_basis = _quoted_unit(unit_text)
        except ValueError:
            pass  # Unsupported physical units (metres etc.) remain unknown-size packages.
        else:
            if unit_basis.unit == Unit.PIECE and unit_basis.amount == 1 and per_unit == current:
                quantity = Quantity(amount=1, unit=Unit.PIECE)
    availability = {"true": "available", "false": "unavailable"}.get(
        str(tile.get("data-auto-available", "")), "unknown"
    )
    standard: dict[str, Any] = {
        "product": {
            "store_id": "tesco",
            "sku": sku,
            "name": name,
            "category": category,
            "quantity": quantity.model_dump() if quantity else None,
            "variable_weight": weighted,
        },
        "offer_key": "standard",
        "scope": SCOPE,
        "currency": "CZK",
        "current_price": current,
        "price_basis": basis.model_dump(),
        "availability": availability,
        "source_url": str(link["href"]),
    }
    offers = [standard]
    promotions = tile.select(".ddsweb-value-bar__content-text")
    terms = tile.select(".ddsweb-value-bar__terms")
    if len(promotions) != len(terms):
        raise ValueError("promotion terms do not cover displayed promotions")
    for label_node, term_node in zip(promotions, terms, strict=True):
        label, term = _text(label_node), _text(term_node)
        end = re.fullmatch(r"Nabídka platí do (\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})", term)
        if not end:
            raise ValueError("unrecognized promotion validity")
        expiry = date(int(end[3]), int(end[2]), int(end[1]))
        if "Clubcard" in label:
            quoted = re.search(rf"({MONEY})\s*Kč(?:/(kg|g|l|ml|kus))?\s+s Clubcard", label)
            if not quoted:
                raise ValueError("unsupported Clubcard purchase conditions")
            # Require a simple price, optionally prefixed by a descriptive savings message.
            prefix = label[: quoted.start()].strip()
            if prefix and re.fullmatch(r"Ušetřete\s+\d+/\d+", prefix) is None:
                raise ValueError("unsupported Clubcard purchase conditions")
            loyalty = copy.deepcopy(standard)
            loyalty.update(
                offer_key="loyalty",
                current_price=_decimal(quoted[1]),
                regular_price=current,
                valid_until=expiry,
                promotion={
                    "kind": "loyalty",
                    "label": label,
                    "requires_loyalty": True,
                    "conditions": "Tesco Clubcard",
                },
            )
            if weighted:
                if quoted[2] not in {"kg", "g"}:
                    raise ValueError("Clubcard offer lacks an explicit mass price")
            if quoted[2]:
                contents = basis if weighted else quantity
                if contents is None:
                    raise ValueError("Clubcard price basis cannot be converted to package contents")
                contents_amount, contents_unit = canonical_quantity(contents.amount, contents.unit)
                quoted_unit = Unit.PIECE if quoted[2] == "kus" else Unit(quoted[2])
                quote_amount, quote_unit = canonical_quantity(Decimal(1), quoted_unit)
                if contents_unit != quote_unit:
                    raise ValueError("Clubcard quote and contents use different physical units")
                loyalty["current_price"] *= contents_amount / quote_amount
            offers.append(loyalty)
        else:
            cut = re.fullmatch(rf"-(\d+(?:[.,]\d+)?)%, předtím ({MONEY}) Kč", label)
            if not cut:
                raise ValueError("unsupported promotional purchase conditions")
            standard.update(
                regular_price=_decimal(cut[2]),
                valid_until=expiry,
                promotion={
                    "kind": "price_cut",
                    "label": label,
                    "advertised_discount_percent": _decimal(cut[1]),
                },
            )
    return offers


@dataclass(frozen=True)
class TescoPage:
    items: list[AcquisitionItem]
    skus: tuple[str, ...]
    total: int
    start: int
    end: int
    next_url: str | None


def parse_page(content: bytes, url: str, fetched_at: datetime, category: str) -> TescoPage:
    soup = BeautifulSoup(content, "lxml")
    count = soup.select_one('[data-testid="pagination-result-count"]')
    match = re.fullmatch(r"Zobrazeno (\d+) až (\d+) z (\d+) produktů", _text(count))
    if match is None:
        raise ValueError("missing exact category coverage count")
    start, end, total = map(int, match.groups())
    tiles = soup.select("#list-content > li")
    if not tiles or end - start + 1 != len(tiles) or not (1 <= start <= end <= total):
        raise ValueError("product grid disagrees with coverage count")
    skus = tuple(str(tile.get("data-testid", "")) for tile in tiles)
    if len(set(skus)) != len(skus) or any(not sku.isascii() or not sku.isdigit() for sku in skus):
        raise ValueError("missing or repeated product IDs")
    next_page = page_number(url, url) + 1
    next_urls = set()
    for anchor in soup.find_all("a", href=True):
        href = urljoin(url, str(anchor["href"]))
        parts = urlsplit(href)
        href = urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
        if parts.path == urlsplit(url).path and parse_qs(parts.query).get("page") == [
            str(next_page)
        ]:
            page_number(href, url)
            next_urls.add(href)
    if end < total and len(next_urls) != 1:
        raise ValueError("missing or ambiguous next-page link")
    if end == total and next_urls:
        raise ValueError("pagination continues after the coverage count")
    items = []
    for tile, sku in zip(tiles, skus, strict=True):
        evidence = SourceEvidence(
            content,
            url,
            "text/html",
            fetched_at,
            f'#list-content > li[data-testid="{sku}"]',
            {
                "source_id": "tesco",
                "parser_version": 1,
                "scope": SCOPE,
                "category": category,
                "snapshot_policy": "public markup; session scripts removed",
                "price_basis_policy": "quoted mass for variable weights; sold package otherwise",
            },
        )
        try:
            offers = _offers(tile, category)
        except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
            items.append(AcquisitionItem(evidence=evidence, error=f"product tile failure: {exc}"))
            continue
        items.extend(AcquisitionItem(evidence=evidence, candidate=offer) for offer in offers)
    return TescoPage(items, skus, total, start, end, next(iter(next_urls), None))
