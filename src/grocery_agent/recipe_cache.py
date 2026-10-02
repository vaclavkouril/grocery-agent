"""Policy-driven acquisition and immutable, source-attributed recipe snapshots."""

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy.exc import SQLAlchemyError

from grocery_agent.acquisition.profiles import profiled_adapter
from grocery_agent.application.runtime import run_acquisition
from grocery_agent.catalogue.config import CatalogueSettings
from grocery_agent.catalogue.models import CatalogueState
from grocery_agent.catalogue.profiles import AcquisitionProfile
from grocery_agent.catalogue.repository import SQLAlchemyCatalogueRepository
from grocery_agent.catalogue.snapshot import pin_states
from grocery_agent.config import Settings
from grocery_agent.meals.catalog import MealCatalog
from grocery_agent.persistence.database import create_database_engine
from grocery_agent.persistence.migrations import require_offer_schema
from grocery_agent.persistence.reader import SQLAlchemyCurrentOfferReader
from grocery_agent.pipeline.results import ScrapeResult
from grocery_agent.recipes import RecipeRequest
from grocery_agent.stores.base import AcquisitionAdapter
from grocery_agent.stores.registry import StoreRegistry, default_registry
from grocery_agent.workflow import writer_lock


class RecipeOptions(Protocol):
    @property
    def source_scopes(self) -> dict[str, tuple[str, ...]]: ...
    @property
    def acquisition_profiles(self) -> tuple[AcquisitionProfile, ...]: ...
    @property
    def provider_timeout_seconds(self) -> int: ...
    @property
    def context_ingredients(self) -> int: ...


Refresh = Callable[[AcquisitionAdapter, AcquisitionProfile], ScrapeResult]


class RecipeCache:
    def __init__(
        self,
        settings: Settings,
        options: RecipeOptions,
        clock: Callable[[], datetime],
        *,
        refresh: Refresh | None = None,
        registry: StoreRegistry | None = None,
    ) -> None:
        self.settings, self.options, self.clock = settings, options, clock
        self.refresh = refresh or self._acquire
        self.registry = registry or default_registry()

    def _acquire(self, adapter: AcquisitionAdapter, profile: AcquisitionProfile) -> ScrapeResult:
        return asyncio.run(run_acquisition(self.settings, [adapter], profiles=[profile]))[0]

    def scopes(self, request: RecipeRequest, catalog: MealCatalog) -> dict[str, tuple[str, ...]]:
        configured = self.options.source_scopes or {
            catalog.policy.source_id: (catalog.policy.scope,)
        }
        if set(request.source_ids) - configured.keys():
            raise ValueError("recipe sources require explicitly configured compatible scopes")
        if any(not configured[source] for source in request.source_ids):
            raise ValueError("each recipe source requires a nonempty compatible scope list")
        return {source: configured[source] for source in request.source_ids}

    def _read(self, source: str, fingerprint: str, max_age: int) -> CatalogueState:
        engine = create_database_engine(self.settings.database_url, read_only=True)
        try:
            require_offer_schema(engine)
            options = CatalogueSettings()
            options = options.model_copy(
                update={"max_age_hours": min(max_age, options.max_age_hours)}
            )
            return SQLAlchemyCatalogueRepository(engine, options).state(
                source, self.clock(), fingerprint
            )
        finally:
            engine.dispose()

    def _cached(self, source: str, fingerprint: str, max_age: int) -> CatalogueState | None:
        try:
            return self._read(source, fingerprint, max_age)
        except (SQLAlchemyError, ValueError, OSError):
            return None

    def _selection(
        self,
        request: RecipeRequest,
    ) -> list[tuple[str, str, AcquisitionAdapter | None, AcquisitionProfile | None]]:
        selected: list[tuple[str, str, AcquisitionAdapter | None, AcquisitionProfile | None]] = []
        configured = self.options.acquisition_profiles
        for source in request.source_ids:
            explicit = request.profile_fingerprints.get(source) or request.profile_fingerprint
            # Cache-only explicit/legacy reads need no source adapter or acquisition setup.
            if request.cache_policy == "cache-only":
                selected.append((source, explicit or "legacy", None, None))
                continue
            profiles = [profile for profile in configured if profile.source_id == source]
            if explicit == "legacy":
                raise ValueError("unknown legacy coverage cannot be refreshed as a named profile")
            if not profiles and explicit is None:
                profiles = [AcquisitionProfile(source_id=source)]
            resolved = [profiled_adapter(self.registry, profile) for profile in profiles]
            matches = [
                (adapter, profile)
                for adapter, profile in resolved
                if (explicit is not None and profile.fingerprint == explicit)
                or (explicit is None and profile.name == "default")
            ]
            if len(matches) != 1:
                # A fully specified fingerprint can still use a fresh published collection
                # in local mode without allowing an unconfigured refresh of that profile.
                if explicit is not None and request.cache_policy == "local":
                    selected.append((source, explicit, None, None))
                    continue
                raise ValueError("select a unique configured acquisition profile for each source")
            adapter, profile = matches[0]
            if profile.coverage != "complete":
                raise ValueError("recipe acquisition requires a complete configured profile")
            selected.append((source, profile.fingerprint, adapter, profile))
        return selected

    def pin(self, request: RecipeRequest, catalog: MealCatalog) -> dict[str, Any]:
        scopes = self.scopes(request, catalog)
        # Validate all selectors before starting any acquisition or opening writable storage.
        selections = self._selection(request)
        states: list[CatalogueState] = []
        extra_warnings: list[str] = []
        for source, fingerprint, adapter, profile in selections:
            previous = self._cached(source, fingerprint, catalog.policy.max_age_hours)
            if request.cache_policy == "cache-only":
                if previous is None:
                    raise ValueError("cached grocery collection is missing, stale or unavailable")
                if len(selections) > 1 and not previous.coverage_complete:
                    raise ValueError("combined recipes require complete known acquisition profiles")
                states.append(previous)
                continue
            if (
                request.cache_policy == "local"
                and previous is not None
                and previous.coverage_complete
            ):
                states.append(previous)
                continue
            if adapter is None or profile is None:
                raise ValueError("missing or stale profile has no configured acquisition source")
            try:
                with writer_lock(self.settings.lock_path, timeout_seconds=30):
                    # Another requester may have refreshed the profile while we waited.
                    current = self._cached(source, fingerprint, catalog.policy.max_age_hours)
                    if request.cache_policy == "local" and current and current.coverage_complete:
                        states.append(current)
                        continue
                    result = self.refresh(adapter, profile)
                    if result.status != "success" or result.profile_fingerprint != fingerprint:
                        raise ValueError("acquisition did not complete the requested profile")
                    current = self._read(source, fingerprint, catalog.policy.max_age_hours)
                    if current.run_id != result.run_id or not current.coverage_complete:
                        raise ValueError(
                            "acquisition did not publish a complete requested collection"
                        )
                    states.append(current)
            except Exception as exc:
                if request.cache_policy == "no-cache":
                    raise RuntimeError(
                        "required grocery refresh failed; cached fallback is disabled"
                    ) from exc
                fallback = self._cached(source, fingerprint, catalog.policy.max_age_hours)
                if fallback is None or not fallback.coverage_complete:
                    raise RuntimeError(
                        "grocery refresh failed and no fresh complete collection remains"
                    ) from exc
                states.append(fallback)
                extra_warnings.append(
                    f"Refresh failed for {source}; using its fresh previous profile collection."
                )
        engine = create_database_engine(self.settings.database_url, read_only=True)
        try:
            require_offer_schema(engine)
            reader = SQLAlchemyCurrentOfferReader(engine)
            offers: list[dict[str, Any]] = []
            for state in states:
                batch = reader.get_batch(state.run_id, state.source_id)
                offers.extend(
                    {
                        "offer": item.offer.model_dump(mode="json", exclude_computed_fields=True),
                        "observed_at": item.observed_at.isoformat(),
                        "observation_id": item.observation_id,
                        "source_id": state.source_id,
                        "run_id": state.run_id,
                    }
                    for item in reader.iter_offers(batch)
                )
            snapshot = pin_states(states)
            scopes_seen = sorted({scope for state in states for scope in state.actual_scopes})
            return {
                "run_id": states[0].run_id,
                "source_id": states[0].source_id,
                "profile_fingerprint": states[0].profile_fingerprint,
                "profile_fingerprints": {
                    state.source_id: state.profile_fingerprint for state in states
                },
                "catalogue_snapshot": snapshot.model_dump(mode="json"),
                "collections": [state.model_dump(mode="json") for state in states],
                "source_scopes": scopes,
                "actual_scope": scopes_seen[0] if len(scopes_seen) == 1 else None,
                "actual_scopes": scopes_seen,
                "coverage_complete": snapshot.complete,
                "finished_at": min(state.batch_finished_at for state in states).isoformat(),
                "warnings": list(snapshot.warnings) + extra_warnings,
                "latest_run_status": states[0].latest_run_status,
                "offers": offers,
            }
        finally:
            engine.dispose()
