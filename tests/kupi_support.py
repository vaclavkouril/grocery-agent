from datetime import UTC, datetime
from pathlib import Path

import httpx

from grocery_agent.stores.kupi.adapter import KupiAdapter
from grocery_agent.stores.kupi.config import KupiSettings

FIXTURES = Path(__file__).parent / "fixtures" / "kupi"
FETCHED_AT = datetime(2026, 9, 30, 12, tzinfo=UTC)
LISTING_URL = "https://www.kupi.cz/slevy/ovoce-a-zelenina"


def adapter(**settings: object) -> KupiAdapter:
    return KupiAdapter(KupiSettings(request_delay_seconds=0, **settings))


def fixture_response(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/robots.txt":
        return httpx.Response(200, content=(FIXTURES / "robots.txt").read_bytes())
    assert request.url.path == "/slevy/ovoce-a-zelenina", f"unexpected HTTP: {request.url}"
    page = request.url.params.get("page", "1")
    assert page in {"1", "2"}, f"unexpected page: {request.url}"
    return httpx.Response(200, content=(FIXTURES / f"page{page}.html").read_bytes())
