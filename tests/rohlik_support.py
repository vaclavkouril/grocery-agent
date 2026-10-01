import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

from grocery_agent.stores.rohlik.adapter import RohlikAdapter
from grocery_agent.stores.rohlik.config import RohlikSettings

FIXTURES = Path(__file__).parent / "fixtures" / "rohlik"
FETCHED_AT = datetime(2026, 10, 1, 12, tzinfo=UTC)
CATEGORY_ID = 300102000


def adapter(**overrides: object) -> RohlikAdapter:
    return RohlikAdapter(
        RohlikSettings.model_validate(
            {
                "category_ids": [CATEGORY_ID],
                "request_delay_seconds": 0,
                "page_size": 4,
                "attempts": 1,
                **overrides,
            }
        )
    )


def fixture_response(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/robots.txt":
        return httpx.Response(200, content=(FIXTURES / "robots.txt").read_bytes())
    if path == "/":
        return httpx.Response(200, content=(FIXTURES / "context.html").read_bytes())
    cards = json.loads((FIXTURES / "cards.json").read_bytes())
    if path.endswith("/count"):
        return httpx.Response(200, json={"results": len(cards)})
    if path.endswith("/products"):
        page, size = int(request.url.params["page"]), int(request.url.params["size"])
        return httpx.Response(
            200,
            json={
                "categoryId": CATEGORY_ID,
                "categoryType": "normal",
                "productIds": [c["productId"] for c in cards[page * size : (page + 1) * size]],
            },
        )
    if path == "/api/v1/products/card":
        ids = {int(v) for v in request.url.params["products"].split(",")}
        return httpx.Response(200, json=[c for c in cards if c["productId"] in ids])
    raise AssertionError(f"unexpected HTTP call: {request.url}")
