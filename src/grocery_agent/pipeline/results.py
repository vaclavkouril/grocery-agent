from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any
from uuid import uuid4

from grocery_agent.models.common import utc_now


@dataclass
class ScrapeResult:
    source_id: str
    run_id: str = field(default_factory=lambda: str(uuid4()))
    started_at: datetime = field(default_factory=utc_now)
    finished_at: datetime | None = None
    status: str = "running"
    fetched: int = 0
    accepted: int = 0
    rejected: int = 0
    changed: int = 0
    errors: int = 0
    error_details: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["started_at"] = self.started_at.isoformat()
        result["finished_at"] = self.finished_at.isoformat() if self.finished_at else None
        return result
