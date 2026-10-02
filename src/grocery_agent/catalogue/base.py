from datetime import datetime
from typing import Protocol, runtime_checkable

from grocery_agent.catalogue.models import CataloguePage, CatalogueQuery, CatalogueState
from grocery_agent.catalogue.profiles import AcquisitionProfile, Coverage


class CataloguePublisher(Protocol):
    def publish(self, run_id: str) -> None: ...


@runtime_checkable
class ProfileCataloguePublisher(CataloguePublisher, Protocol):
    def record_profile(
        self, run_id: str, profile: AcquisitionProfile, coverage: Coverage
    ) -> None: ...


class CatalogueReader(Protocol):
    def state(
        self, source_id: str, now: datetime, profile_fingerprint: str | None = None
    ) -> CatalogueState: ...

    def search(self, query: CatalogueQuery, now: datetime) -> CataloguePage: ...
