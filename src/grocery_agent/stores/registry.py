from collections.abc import Callable

from pydantic import TypeAdapter

from grocery_agent.models.common import StoreId
from grocery_agent.stores.base import AcquisitionAdapter


class StoreRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, Callable[[], AcquisitionAdapter]] = {}

    def register(self, store_id: str, factory: Callable[[], AcquisitionAdapter]) -> None:
        TypeAdapter(StoreId).validate_python(store_id)
        if store_id in self._factories:
            raise ValueError(f"store already registered: {store_id}")
        self._factories[store_id] = factory

    def create(self, store_id: str) -> AcquisitionAdapter:
        if store_id not in self._factories:
            raise ValueError(f"unknown store {store_id!r}; available: {', '.join(self.ids())}")
        adapter = self._factories[store_id]()
        if adapter.source_id != store_id:
            raise ValueError("adapter source_id does not match registry key")
        return adapter

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._factories))


def default_registry() -> StoreRegistry:
    from grocery_agent.stores.kupi.adapter import KupiAdapter
    from grocery_agent.stores.mock.adapter import MockStore

    registry = StoreRegistry()
    registry.register("mock", MockStore)
    registry.register("kupi", KupiAdapter)
    return registry
