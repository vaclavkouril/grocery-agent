"""Immutable acquisition profiles and cache selection rules.

Profiles are intentionally independent from retailer adapters.  The fingerprint is
stable across processes, so a narrow collection can never be mistaken for a full one.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import Field, JsonValue, field_validator

from grocery_agent.models.common import DomainModel, StoreId


class AcquisitionProfile(DomainModel):
    source_id: StoreId
    name: StoreId = "default"
    scope: str | None = None
    categories: tuple[str, ...] = ()
    search: tuple[str, ...] = ()
    retailer_ids: tuple[StoreId, ...] = ()
    eligibility: tuple[str, ...] = ()
    coverage: Literal["complete", "partial"] = "complete"
    options: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("categories", "search", "retailer_ids", "eligibility")
    @classmethod
    def canonical_selectors(cls, values: tuple[str, ...], info: Any) -> tuple[str, ...]:
        normalized = []
        for value in values:
            value = value.strip()
            if info.field_name == "search":
                value = " ".join(unicodedata.normalize("NFKC", value).casefold().split())
            if not value or len(value) > 500:
                raise ValueError("profile selectors must be nonempty and bounded")
            if info.field_name == "eligibility" and value not in {
                "exact",
                "available",
                "non-loyalty",
            }:
                raise ValueError("unsupported acquisition eligibility")
            normalized.append(value)
        return tuple(sorted(set(normalized)))

    @field_validator("scope")
    @classmethod
    def valid_scope(cls, value: str | None) -> str | None:
        if value is not None and (not value or len(value) > 500):
            raise ValueError("profile scope must be nonempty and bounded")
        return value

    @field_validator("options", mode="before")
    @classmethod
    def safe_options(cls, value: Any) -> Any:
        allowed = {
            "listing_url",
            "category_slugs",
            "category_paths",
            "category_ids",
            "max_pages",
            "page_size",
            "expected_warehouse_id",
            "account_scope",
            "store_name",
        }
        if not isinstance(value, dict) or set(value) - allowed:
            raise ValueError("unsupported acquisition options")

        def reject_floats(item: Any) -> None:
            if isinstance(item, float):
                raise ValueError("acquisition options cannot contain floats")
            if isinstance(item, dict):
                for child in item.values():
                    reject_floats(child)
            elif isinstance(item, (tuple, list)):
                for child in item:
                    reject_floats(child)

        reject_floats(value)
        result = dict(value)
        for key in {"category_slugs", "category_paths", "category_ids"} & result.keys():
            items = result[key]
            if items is None and key == "category_slugs":
                continue
            kind = int if key == "category_ids" else str
            if not isinstance(items, (list, tuple)) or not items:
                raise ValueError("native categories must be a nonempty list")
            if any(type(item) is not kind for item in items):
                raise ValueError("native category identifiers have an invalid type")
            result[key] = sorted(set(items))
        return result

    @property
    def fingerprint(self) -> str:
        payload = self.model_dump(mode="json")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()[:24]


class CachePolicy(DomainModel):
    mode: Literal["local", "refresh", "no-cache", "cache-only"] = "local"
    max_age_hours: int = Field(default=36, ge=1, le=168)

    def usable(self, *, finished_at: datetime, now: datetime, complete: bool) -> bool:
        if self.mode == "no-cache" or not complete:
            return False
        return finished_at <= now <= finished_at + timedelta(hours=self.max_age_hours)


class Coverage(DomainModel):
    profile_fingerprint: str
    expected: int | None = None
    observed: int = 0
    complete: bool = False
    actual_scope: str | None = None
    actual_scopes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def require_publishable(self) -> None:
        if not self.complete:
            raise ValueError("incomplete acquisition coverage cannot be published")
        if self.expected is not None and self.observed < self.expected:
            raise ValueError("acquisition coverage is below the expected item count")


def combined_profile(profiles: tuple[AcquisitionProfile, ...]) -> str:
    """Return a stable identity for a compatible set of source profiles."""
    if not profiles:
        raise ValueError("at least one acquisition profile is required")
    if len({p.source_id for p in profiles}) != len(profiles):
        raise ValueError("a combined acquisition may contain one profile per source")
    encoded = "|".join(sorted(p.fingerprint for p in profiles)).encode()
    return hashlib.sha256(encoded).hexdigest()[:24]
