import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from grocery_agent.stores.base import AcquisitionAdapter, AdapterContext
from grocery_agent.stores.mock.adapter import MockStore
from grocery_agent.stores.mock.parser import parse_offers
from grocery_agent.stores.registry import StoreRegistry, default_registry
from tests.adapter_contract import assert_adapter_contract
from tests.kupi_support import FETCHED_AT, fixture_response
from tests.kupi_support import adapter as kupi_adapter

CONTRACT_CASES: dict[str, tuple[Callable[[], AcquisitionAdapter], int]] = {
    "mock": (MockStore, 5),
    "kupi": (kupi_adapter, 90),
}


@pytest.mark.parametrize("store_id", sorted(CONTRACT_CASES))
async def test_adapter_contract(store_id: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("grocery_agent.stores.kupi.adapter.utc_now", lambda: FETCHED_AT)

    def unexpected_http(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected HTTP call: {request.url}")

    transport = fixture_response if store_id == "kupi" else unexpected_http
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        factory, count = CONTRACT_CASES[store_id]
        await assert_adapter_contract(factory, AdapterContext(client), expected_count=count)


def test_every_registered_adapter_has_contract_case() -> None:
    assert set(default_registry().ids()) == set(CONTRACT_CASES)


def test_registry_rejects_duplicates_and_unknown_stores() -> None:
    registry = default_registry()
    with pytest.raises(ValueError, match="already registered"):
        registry.register("mock", MockStore)
    with pytest.raises(ValueError, match="unknown store"):
        registry.create("albert")


def test_registry_rejects_invalid_and_mismatched_id() -> None:
    registry = StoreRegistry()
    with pytest.raises(ValueError):
        registry.register("../bad", MockStore)
    registry.register("other", MockStore)
    with pytest.raises(ValueError, match="does not match"):
        registry.create("other")


def test_parser_retains_raw_fields_but_does_not_leak_them() -> None:
    content = MockStore()._read_fixture()
    item = next(parse_offers(content, datetime.now(UTC)))
    assert b"supplier_note" in item.evidence.content
    assert "supplier_note" not in str(item.candidate)


def test_parser_continues_after_malformed_record() -> None:
    source = json.loads(MockStore()._read_fixture())
    content = json.dumps([{}, source[0], "not an object"]).encode()
    items = list(parse_offers(content, datetime.now(UTC)))
    assert len(items) == 3
    assert items[0].error and items[2].error
    assert items[1].candidate
    assert all(item.evidence.content == content for item in items)


@pytest.mark.parametrize("content", [b"{broken", b"{}"])
def test_parser_raises_on_broken_source(content: bytes) -> None:
    with pytest.raises(ValueError):
        list(parse_offers(content, datetime.now(UTC)))


async def test_custom_fixture_path(tmp_path: Path) -> None:
    path = tmp_path / "offers.json"
    path.write_text("[]")
    async with httpx.AsyncClient() as client:
        assert [item async for item in MockStore(path).fetch_offers(AdapterContext(client))] == []
