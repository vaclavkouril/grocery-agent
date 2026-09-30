import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine

from grocery_agent.models.common import utc_now
from grocery_agent.persistence.database import open_database
from grocery_agent.persistence.repository import SQLAlchemyOfferRepository
from grocery_agent.persistence.snapshots import FileSnapshotStore
from grocery_agent.stores.mock.adapter import MockStore
from grocery_agent.stores.mock.parser import parse_offers


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    if request.node.get_closest_marker("live"):
        return

    def denied(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("network access is forbidden in offline tests")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    value = open_database(f"sqlite:///{tmp_path / 'test.db'}")
    yield value
    value.dispose()


@pytest.fixture
def repository(engine: Engine) -> SQLAlchemyOfferRepository:
    return SQLAlchemyOfferRepository(engine)


@pytest.fixture
def snapshots(tmp_path: Path) -> FileSnapshotStore:
    return FileSnapshotStore(tmp_path / "snapshots")


@pytest.fixture
def candidate() -> dict[str, Any]:
    item = next(parse_offers(MockStore()._read_fixture(), utc_now()))
    assert item.candidate is not None
    return item.candidate
