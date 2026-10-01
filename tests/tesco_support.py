from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

from grocery_agent.stores.base import AdapterContext
from grocery_agent.stores.tesco.adapter import FetchPage, TescoAdapter
from grocery_agent.stores.tesco.config import TescoSettings
from grocery_agent.stores.tesco.parser import category_url

FIXTURES = Path(__file__).parent / "fixtures" / "tesco"
LISTING_URL = category_url("ovoce-a-zelenina")


def fixture_response(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/robots.txt":
        return httpx.Response(200, content=(FIXTURES / "robots.txt").read_bytes())
    assert request.url.path == "/shop/cs-CZ/browse/ovoce-a-zelenina/all"
    number = int(request.url.params.get("page", "1"))
    assert number in {1, 2}
    soup = BeautifulSoup((FIXTURES / "produce.html").read_bytes(), "lxml")
    grid = soup.select_one("#list-content")
    assert grid is not None
    tiles = grid.select(":scope > li")
    # Two pages around the 24 captured tiles. Only counts and page layout are synthetic.
    for tile in tiles[:12] if number == 2 else tiles[12:]:
        tile.decompose()
    count = soup.select_one('[data-testid="pagination-result-count"]')
    assert count is not None
    count.string = (
        f"Zobrazeno {1 if number == 1 else 13} až {12 if number == 1 else 24} z 24 produktů"
    )
    for anchor in soup.find_all("a", href=True):
        if "?" in str(anchor["href"]):
            anchor.decompose()
    if number == 1:
        link = soup.new_tag("a", href=LISTING_URL + "?sortBy=relevance&page=2&count=24")
        link.string = "2"
        assert soup.body
        soup.body.append(link)
    return httpx.Response(200, content=str(soup).encode())


class FixtureTesco(TescoAdapter):
    @asynccontextmanager
    async def _session(self, context: AdapterContext) -> AsyncIterator[FetchPage]:
        async def fetch(url: str) -> httpx.Response:
            return await context.http.get(url, follow_redirects=False)

        yield fetch


def adapter(**overrides: object) -> TescoAdapter:
    return FixtureTesco(
        TescoSettings.model_validate(
            {
                "category_slugs": ["ovoce-a-zelenina"],
                "request_delay_seconds": 0,
                "attempts": 1,
                **overrides,
            }
        )
    )
