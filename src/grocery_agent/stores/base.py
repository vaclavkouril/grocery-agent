from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from pydantic import JsonValue


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


class StoreAdapter(ABC):
    """An adapter owns acquisition/parsing, never SQL or cross-store matching."""

    @property
    @abstractmethod
    def store_id(self) -> str: ...

    @abstractmethod
    def fetch_offers(self, context: AdapterContext) -> AsyncIterator[AcquisitionItem]:
        """Stream canonical candidates or explicit item failures; raise on source failure."""
        ...
