import json
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from grocery_agent.stores.base import AcquisitionItem, SourceEvidence


def parse_offers(content: bytes, fetched_at: datetime) -> Iterator[AcquisitionItem]:
    """Pure parser: accepts saved bytes; performs no network or storage operations."""
    records = json.loads(content)
    if not isinstance(records, list):
        raise ValueError("mock source must contain a JSON array")
    for index, record in enumerate(records):
        evidence = SourceEvidence(
            content=content,
            url="https://mock.example.invalid/offers",
            media_type="application/json",
            fetched_at=fetched_at,
            locator=f"$[{index}]",
            metadata={"parser_version": "1"},
        )
        try:
            if not isinstance(record, dict):
                raise ValueError("record must be an object")
            candidate: dict[str, Any] = {
                "product": {
                    "store_id": "mock",
                    "sku": record["id"],
                    "name": record["title"],
                    "brand": record.get("brand"),
                    "category": record.get("category"),
                    "gtin": record.get("ean"),
                    "quantity": _quantity(record.get("size")),
                    "variable_weight": record.get("variable", False),
                },
                "offer_key": record.get("variant", "standard"),
                "scope": "national",
                "current_price": record["price"],
                "regular_price": record.get("was"),
                "currency": "CZK",
                "price_basis": _quantity(record["basis"]),
                "promotion": record.get("promo"),
                "valid_from": record.get("starts"),
                "valid_until": record.get("ends"),
                "availability": {True: "available", False: "unavailable", None: "unknown"}[
                    record.get("available")
                ],
                "source_url": f"https://mock.example.invalid/products/{record['id']}",
            }
        except (KeyError, TypeError, ValueError) as exc:
            yield AcquisitionItem(evidence=evidence, error=f"parser: {type(exc).__name__}: {exc}")
        else:
            yield AcquisitionItem(evidence=evidence, candidate=candidate)


def _quantity(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    return {"amount": raw["value"], "unit": raw["unit"]} if raw is not None else None
