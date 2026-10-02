"""Native acquisition selectors followed by transparent quote filtering.

Search phrases use NFKC, casefold and collapsed whitespace, and must ALL occur in
the product name/brand. Retailer IDs are inclusive (ANY); eligibility rules are ALL.
Categories select native acquisition endpoints, never product category labels.
"""

import re
import unicodedata
from collections.abc import AsyncIterator, Callable
from typing import Any, cast

from pydantic_settings import BaseSettings

from grocery_agent.catalogue.profiles import AcquisitionProfile
from grocery_agent.models.offer import Availability, Offer, PriceQualifier
from grocery_agent.stores.base import AcquisitionAdapter, AcquisitionItem, AdapterContext
from grocery_agent.stores.registry import StoreRegistry


def _native(adapter: AcquisitionAdapter) -> tuple[str | None, tuple[str, ...]]:
    # Only known constructors/settings contracts may be rebuilt. Custom adapters
    # do not acquire native selectors merely by exposing similarly named attributes.
    from grocery_agent.stores.kupi.adapter import KupiAdapter
    from grocery_agent.stores.makro.adapter import MakroAdapter
    from grocery_agent.stores.rohlik.adapter import RohlikAdapter
    from grocery_agent.stores.tesco.adapter import TescoAdapter

    contracts = {
        KupiAdapter: ("category_slugs", ("listing_url", "max_pages")),
        MakroAdapter: ("category_paths", ("account_scope", "store_name", "max_pages")),
        RohlikAdapter: ("category_ids", ("page_size", "max_pages", "expected_warehouse_id")),
        TescoAdapter: ("category_slugs", ("max_pages",)),
    }
    return contracts.get(type(adapter), (None, ()))


def _settings(adapter: AcquisitionAdapter, requested: AcquisitionProfile) -> BaseSettings | None:
    category_field, fields = _native(adapter)
    if category_field is None:
        if requested.categories or requested.options:
            raise ValueError("adapter has no supported native category/options configuration")
        return None
    allowed = {category_field, *fields}
    if set(requested.options) - allowed:
        raise ValueError("unsupported options for this acquisition source")
    updates: dict[str, Any] = dict(requested.options)
    for key, value in updates.items():
        if key in {"max_pages", "page_size", "expected_warehouse_id"}:
            if value is None and key == "expected_warehouse_id":
                continue
            if type(value) is not int:
                raise ValueError(f"{key} must be an integer")
        elif key in {"account_scope", "store_name", "listing_url"}:
            if value is None and key != "account_scope":
                continue
            if not isinstance(value, str) or not value.strip() or len(value) > 500:
                raise ValueError(f"{key} must be a nonempty bounded string")
    if adapter.source_id == "kupi" and category_field in updates and "listing_url" not in updates:
        updates["listing_url"] = None
    if requested.categories:
        categories: tuple[str, ...] | tuple[int, ...] = requested.categories
        if category_field == "category_ids":
            if any(re.fullmatch(r"[1-9][0-9]*", c) is None for c in requested.categories):
                raise ValueError("Rohlik categories must be positive native integer IDs")
            categories = tuple(sorted({int(c) for c in requested.categories}))
        if category_field in updates and updates[category_field] != sorted(categories):
            raise ValueError("categories and native category options disagree")
        updates[category_field] = categories
        if adapter.source_id == "kupi":
            if updates.get("listing_url") is not None:
                if categories != (updates["listing_url"].rsplit("/", 1)[-1],):
                    raise ValueError("listing_url and categories disagree")
            else:
                updates["listing_url"] = None
    if updates.get(category_field) is not None and category_field in updates:
        updates[category_field] = tuple(updates[category_field])
    settings = cast(Any, adapter).settings
    assert isinstance(settings, BaseSettings)
    copied = settings.model_copy(update=updates)
    # model_copy does not validate updates; all original fields are explicit so
    # settings validation cannot replace the configured base with environment values.
    return type(settings).model_validate(copied.model_dump())


def effective_profile(
    adapter: AcquisitionAdapter, requested: AcquisitionProfile | None = None
) -> AcquisitionProfile:
    """Resolve safe native selectors against this configured source, without fetching."""
    return _resolve(adapter, requested)[0]


def _resolve(
    adapter: AcquisitionAdapter, requested: AcquisitionProfile | None
) -> tuple[AcquisitionProfile, BaseSettings | None]:
    if requested is None and isinstance(adapter, FilteringAdapter):
        requested = adapter.profile
    while isinstance(adapter, FilteringAdapter):
        adapter = adapter.adapter
    requested = AcquisitionProfile.model_validate(
        requested.model_dump() if requested is not None else {"source_id": adapter.source_id}
    )
    if requested.source_id != adapter.source_id:
        raise ValueError("profile source_id does not match adapter")
    settings = _settings(adapter, requested)
    if settings is None:
        return requested, None
    category_field, fields = _native(adapter)
    assert category_field is not None
    options = {key: settings.model_dump(mode="json")[key] for key in (category_field, *fields)}
    categories = getattr(settings, category_field) or ()
    listing_url = getattr(settings, "listing_url", None)
    if adapter.source_id == "kupi" and listing_url is not None:
        categories = (listing_url.rsplit("/", 1)[-1],)
        # Inactive category defaults must not alter the identity of a single URL scrape.
        options[category_field] = list(categories)
    effective = AcquisitionProfile.model_validate(
        {
            **requested.model_dump(),
            "categories": tuple(str(c) for c in categories),
            "options": options,
        }
    )
    return effective, settings


class FilteringAdapter(AcquisitionAdapter):
    """Retain original items/evidence; inspect validated quotes only for selection."""

    def __init__(self, adapter: AcquisitionAdapter, profile: AcquisitionProfile) -> None:
        self.adapter = adapter
        self.profile = AcquisitionProfile.model_validate(profile.model_dump())
        if self.profile.source_id != adapter.source_id:
            raise ValueError("profile source_id does not match adapter")
        self._actual_scopes: set[str] = set()
        self.complete = False

    @property
    def source_id(self) -> str:
        return self.adapter.source_id

    @property
    def actual_scopes(self) -> tuple[str, ...]:
        return tuple(sorted(self._actual_scopes))

    def validate_offer(self, offer: Offer) -> None:
        self.adapter.validate_offer(offer)

    def _matches(self, offer: Offer) -> bool:
        profile = self.profile
        if profile.retailer_ids and offer.product.store_id not in profile.retailer_ids:
            return False
        if profile.scope is not None and offer.scope != profile.scope:
            return False
        text = " ".join(
            unicodedata.normalize("NFKC", f"{offer.product.name} {offer.product.brand or ''}")
            .casefold()
            .split()
        )
        if any(phrase not in text for phrase in profile.search):
            return False
        for rule in profile.eligibility:
            if rule == "exact" and offer.price_qualifier != PriceQualifier.EXACT:
                return False
            if rule == "available" and offer.availability != Availability.AVAILABLE:
                return False
            if rule == "non-loyalty" and offer.promotion and offer.promotion.requires_loyalty:
                return False
        return True

    async def fetch_offers(self, context: AdapterContext) -> AsyncIterator[AcquisitionItem]:
        self.complete = False
        self._actual_scopes.clear()
        valid = True
        async for item in self.adapter.fetch_offers(context):
            if item.error is not None:
                valid = False
                yield item
                continue
            try:
                offer = Offer.model_validate(item.candidate)
                self.adapter.validate_offer(offer)
            except Exception:
                # Source-specific validation failures belong to the pipeline too.
                valid = False
                yield item
                continue
            if self._matches(offer):
                self._actual_scopes.add(offer.scope)
                yield item
        self.complete = valid and self.profile.coverage == "complete"


def profile_adapter(
    adapter: AcquisitionAdapter, requested: AcquisitionProfile
) -> tuple[AcquisitionAdapter, AcquisitionProfile]:
    """Apply native overrides and quote filters to an existing configured source.

    Reapplying the same effective identity reuses the wrapper; a different profile
    starts from its underlying configured native adapter, without nesting filters.
    """
    original = adapter
    while isinstance(adapter, FilteringAdapter):
        adapter = adapter.adapter
    effective, settings = _resolve(adapter, requested)
    if (
        isinstance(original, FilteringAdapter)
        and original.adapter is adapter
        and original.profile.fingerprint == effective.fingerprint
    ):
        return original, effective
    if settings is not None:
        constructor = cast(Callable[..., AcquisitionAdapter], type(adapter))
        adapter = constructor(settings=settings)
    return FilteringAdapter(adapter, effective), effective


def profiled_adapter(
    registry: StoreRegistry, profile: AcquisitionProfile
) -> tuple[AcquisitionAdapter, AcquisitionProfile]:
    """Create and profile a registered source with a single configuration pass."""
    return profile_adapter(registry.create(profile.source_id), profile)
