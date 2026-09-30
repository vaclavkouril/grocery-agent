import re
import unicodedata
from decimal import Decimal


def normalize_name(name: str) -> str:
    """Stable lexical normalization, not an identity or fuzzy matching algorithm."""
    text = unicodedata.normalize("NFKC", name).casefold()
    return " ".join(re.sub(r"[^\w\s]|_", " ", text).split())


def normalize_gtin(value: str) -> str:
    """Validate a GTIN check digit and pad to its equivalent 14-digit identity."""
    if len(value) not in {8, 12, 13, 14} or not value.isascii() or not value.isdigit():
        raise ValueError("GTIN must contain 8, 12, 13, or 14 ASCII digits")
    weighted = sum(
        int(digit) * (3 if i % 2 == 0 else 1) for i, digit in enumerate(reversed(value[:-1]))
    )
    if (10 - weighted % 10) % 10 != int(value[-1]):
        raise ValueError("invalid GTIN check digit")
    return value.zfill(14)


def canonical_quantity(amount: Decimal, unit: str) -> tuple[Decimal, str]:
    if unit == "g":
        return amount / Decimal(1000), "kg"
    if unit == "ml":
        return amount / Decimal(1000), "l"
    return amount, unit
