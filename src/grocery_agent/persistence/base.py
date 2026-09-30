from dataclasses import dataclass
from typing import Any, Protocol

from grocery_agent.models.observation import PriceObservation
from grocery_agent.models.product import Product
from grocery_agent.pipeline.results import ScrapeResult
from grocery_agent.stores.base import SourceEvidence


@dataclass(frozen=True)
class StoredSnapshot:
    id: str
    sha256: str
    relative_path: str
    evidence: SourceEvidence


class SnapshotStore(Protocol):
    def save(self, evidence: SourceEvidence) -> StoredSnapshot: ...


class OfferRepository(Protocol):
    def start_run(self, result: ScrapeResult) -> None: ...

    def finish_run(self, result: ScrapeResult) -> None: ...

    def add_snapshot(self, run_id: str, snapshot: StoredSnapshot) -> None: ...

    def accept(self, run_id: str, observation: PriceObservation, product: Product | None) -> bool:
        """Atomically save state and provenance; return whether the state changed."""
        ...

    def reject(self, run_id: str, snapshot_id: str, details: str) -> None: ...

    def recent_runs(self, limit: int) -> list[dict[str, Any]]: ...
