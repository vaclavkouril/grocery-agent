from datetime import datetime
from typing import Protocol

from grocery_agent.catalogue.models import CataloguePage, CatalogueQuery, CatalogueState


class CataloguePublisher(Protocol):
    def publish(self, run_id: str) -> None: ...


class CatalogueReader(Protocol):
    def state(self, source_id: str, now: datetime) -> CatalogueState: ...

    def search(self, query: CatalogueQuery, now: datetime) -> CataloguePage: ...
