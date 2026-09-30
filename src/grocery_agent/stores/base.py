from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from pydantic import JsonValue

from grocery_agent.models.offer import Offer


@dataclass(frozen=True)
class SourceEvidence:
    content: bytes
    url: str
    media_type: str
    fetched_at: datetime
    locator: str
    metadata: dict[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True)
class AcquisitionItem:
    """Exactly one candidate or parser error, with retained evidence in either case."""

    evidence: SourceEvidence
    candidate: dict[str, Any] | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if (self.candidate is None) == (self.error is None):
            raise ValueError("supply exactly one of candidate or error")


@dataclass(frozen=True)
class AdapterContext:
    http: httpx.AsyncClient


class AcquisitionAdapter(ABC):
    """An adapter owns acquisition/parsing, never SQL or cross-store matching."""

    @property
    @abstractmethod
    def source_id(self) -> str: ...

    @abstractmethod
    def validate_offer(self, offer: Offer) -> None:
        """Reject candidates outside the source's identity boundary."""
        ...

    @abstractmethod
    def fetch_offers(self, context: AdapterContext) -> AsyncIterator[AcquisitionItem]:
        """Stream canonical candidates or explicit item failures; raise on source failure."""
        ...


class StoreAdapter(AcquisitionAdapter):
    """Convenience contract for a source representing exactly one retailer."""

    @property
    @abstractmethod
    def store_id(self) -> str: ...

    @property
    def source_id(self) -> str:
        return self.store_id

    def validate_offer(self, offer: Offer) -> None:
        if offer.product.store_id != self.store_id:
            raise ValueError("candidate store_id does not match the adapter")
