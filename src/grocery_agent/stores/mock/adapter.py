import asyncio
from collections.abc import AsyncIterator
from importlib.resources import files
from pathlib import Path

from grocery_agent.models.common import utc_now
from grocery_agent.stores.base import AcquisitionItem, AdapterContext, StoreAdapter
from grocery_agent.stores.mock.parser import parse_offers


class MockStore(StoreAdapter):
    def __init__(self, fixture_path: Path | None = None) -> None:
        self.fixture_path = fixture_path

    @property
    def store_id(self) -> str:
        return "mock"

    async def fetch_offers(self, context: AdapterContext) -> AsyncIterator[AcquisitionItem]:
        content = await asyncio.to_thread(self._read_fixture)
        for item in parse_offers(content, utc_now()):
            yield item
            await asyncio.sleep(0)

    def _read_fixture(self) -> bytes:
        if self.fixture_path is not None:
            return self.fixture_path.read_bytes()
        return files("grocery_agent.stores.mock").joinpath("offers.json").read_bytes()
