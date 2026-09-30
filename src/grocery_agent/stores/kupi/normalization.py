import re
import unicodedata
from datetime import date, timedelta
from decimal import Decimal

from grocery_agent.models.product import Quantity, Unit


def clean_text(value: str) -> str:
    return " ".join(value.split())


def slug(value: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    result = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    if not result:
        raise ValueError("missing usable locality/retailer")
    return result


def czech_decimal(value: str) -> Decimal:
    text = re.sub(r"\s+", "", value).replace("Kč", "").replace(",", ".")
    if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", text):
        raise ValueError(f"invalid decimal: {value!r}")
    return Decimal(text)


def parse_quantity(value: str) -> Quantity:
    text = clean_text(value).lstrip("/ ")
    match = re.fullmatch(
        r"(?:(\d+)\s*[x×]\s*)?(\d+(?:[.,]\d+)?)\s*(kg|g|l|ml|ks|bal|dávek|dávka|dávky)",
        text,
        re.I,
    )
    if not match:
        raise ValueError(f"unrecognized price basis: {value!r}")
    units = {
        "ks": Unit.PIECE,
        "bal": Unit.PACKAGE,
        "dávek": Unit.SERVING,
        "dávka": Unit.SERVING,
        "dávky": Unit.SERVING,
    }
    unit = units.get(match[3].lower()) or Unit(match[3].lower())
    count = czech_decimal(match[1]) if match[1] is not None else 1
    return Quantity(amount=count * czech_decimal(match[2]), unit=unit)


def parse_validity(value: str, today: date) -> tuple[date | None, date | None]:
    """Inclusive Czech dates. Missing years use the nearest year within six months."""
    text = clean_text(value).lower()
    if text == "aktuální":
        return None, None
    if text == "dnes končí":
        return None, today
    if text == "zítra končí":
        return None, today + timedelta(days=1)
    dates = re.findall(r"(?<!\d)(\d{1,2})\.\s*(\d{1,2})\.(?:\s*(\d{4}))?", text)
    if len(dates) not in {1, 2}:
        raise ValueError(f"unrecognized validity: {value!r}")

    def nearest(parts: tuple[str, str, str], anchor: date) -> date:
        day, month, year = parts
        if year:
            return date(int(year), int(month), int(day))
        candidates = []
        for inferred_year in range(anchor.year - 1, anchor.year + 2):
            try:
                candidates.append(date(inferred_year, int(month), int(day)))
            except ValueError:
                continue
        if not candidates:
            raise ValueError("invalid calendar date")
        result = min(candidates, key=lambda candidate: abs((candidate - anchor).days))
        if abs((result - anchor).days) > 183:
            raise ValueError("ambiguous validity year")
        return result

    first = nearest(dates[0], today)
    if len(dates) == 1:
        if re.match(r"(?:ve?\s+)?(?:po|út|st|čt|pá|so|ne)\s+\d", text):
            return first, first
        if not text.startswith("platí do"):
            raise ValueError("single validity date must explicitly mean valid until")
        return None, first
    end = nearest(dates[1], first)
    if end < first:
        raise ValueError("reversed validity interval")
    return first, end
