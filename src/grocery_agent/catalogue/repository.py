"""Materialized public catalogue published only from complete canonical batches."""

from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, delete, false, func, insert, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from grocery_agent.catalogue.config import CatalogueSettings
from grocery_agent.catalogue.models import (
    CatalogueItem,
    CataloguePage,
    CatalogueQuery,
    CatalogueState,
)
from grocery_agent.catalogue.profiles import AcquisitionProfile, Coverage
from grocery_agent.catalogue.schema import (
    CatalogueEntryRow,
    CatalogueHeadRow,
    CatalogueProfileHeadRow,
    CatalogueRunProfileRow,
)
from grocery_agent.models.common import utc_now
from grocery_agent.models.normalization import normalize_name
from grocery_agent.models.offer import Offer
from grocery_agent.persistence.reader import (
    BatchOfferReader,
    ObservedOffer,
    OfferBatch,
    SQLAlchemyCurrentOfferReader,
)
from grocery_agent.persistence.schema import ObservationRow, ScrapeRunRow


class CatalogueNotReady(ValueError):
    pass


class SQLAlchemyCatalogueRepository:
    def __init__(self, engine: Engine, settings: CatalogueSettings | None = None) -> None:
        self.sessions = sessionmaker(engine)
        self.reader = SQLAlchemyCurrentOfferReader(engine)
        self.settings = settings or CatalogueSettings()

    def record_profile(self, run_id: str, profile: AcquisitionProfile, coverage: Coverage) -> None:
        """Finalize recorded coverage using actual persisted canonical membership, not a label."""
        if coverage.profile_fingerprint != profile.fingerprint:
            raise ValueError("coverage fingerprint does not match profile")
        with self.sessions.begin() as session:
            session.execute(
                update(CatalogueRunProfileRow)
                .where(CatalogueRunProfileRow.run_id == run_id)
                .values(source_id=CatalogueRunProfileRow.source_id)
            )
            run = session.get(ScrapeRunRow, run_id)
            if run is None or run.source_id != profile.source_id:
                raise ValueError("profile source does not match recorded run")
            existing = session.get(CatalogueRunProfileRow, run_id)
            if existing and existing.profile_fingerprint not in {"legacy", profile.fingerprint}:
                raise ValueError("a run cannot be assigned to another profile")
            batch = OfferBatch(run_id, run.source_id, run.finished_at or run.started_at)
            observed = tuple(self.reader.iter_offers(batch))
            scopes = tuple(sorted({item.offer.scope for item in observed}))
            if coverage.observed not in {run.accepted, len(observed)}:
                raise ValueError("coverage observed count does not match recorded membership")
            supplied_scopes = tuple(getattr(coverage, "actual_scopes", ()))
            if supplied_scopes and set(supplied_scopes) != set(scopes):
                raise ValueError("coverage actual scopes do not match recorded offers")
            if coverage.actual_scope is not None and scopes != (coverage.actual_scope,):
                raise ValueError("coverage actual scope does not match recorded offers")
            if profile.scope is not None and any(scope != profile.scope for scope in scopes):
                raise ValueError("recorded offers are outside the requested profile scope")
            complete = (
                coverage.complete
                and profile.coverage == "complete"
                and run.status == "success"
                and run.finished_at is not None
                and bool(observed)
            )
            normalized = Coverage.model_validate(
                {
                    **coverage.model_dump(),
                    "observed": len(observed),
                    "complete": complete,
                    "actual_scope": scopes[0] if len(scopes) == 1 else None,
                    "actual_scopes": scopes,
                }
            ).model_dump(mode="json")
            payload = profile.model_dump(mode="json")
            published = session.scalar(
                select(CatalogueProfileHeadRow.run_id).where(
                    CatalogueProfileHeadRow.run_id == run_id
                )
            )
            if published:
                if not existing or (existing.profile, existing.coverage) != (payload, normalized):
                    raise ValueError("published profile metadata is immutable")
                return
            if existing is None:
                session.add(
                    CatalogueRunProfileRow(
                        run_id=run_id,
                        source_id=run.source_id,
                        profile_fingerprint=profile.fingerprint,
                        profile=payload,
                        coverage=normalized,
                    )
                )
            else:
                existing.profile_fingerprint, existing.profile, existing.coverage = (
                    profile.fingerprint,
                    payload,
                    normalized,
                )

    def collections(self, source_id: str | None = None) -> list[dict[str, Any]]:
        """List published identities, including stale/legacy ones, without claiming freshness."""
        with self.sessions() as session:
            statement = (
                select(CatalogueProfileHeadRow, CatalogueRunProfileRow, ScrapeRunRow)
                .join(
                    CatalogueRunProfileRow,
                    CatalogueRunProfileRow.run_id == CatalogueProfileHeadRow.run_id,
                )
                .join(ScrapeRunRow, ScrapeRunRow.id == CatalogueProfileHeadRow.run_id)
            )
            if source_id is not None:
                statement = statement.where(CatalogueProfileHeadRow.source_id == source_id)
            return [
                {
                    "source_id": head.source_id,
                    "profile_fingerprint": head.profile_fingerprint,
                    "run_id": head.run_id,
                    "published_at": head.published_at.isoformat(),
                    "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                    "profile": metadata.profile,
                    "coverage": metadata.coverage,
                }
                for head, metadata, run in session.execute(
                    statement.order_by(
                        CatalogueProfileHeadRow.source_id,
                        CatalogueProfileHeadRow.profile_fingerprint,
                    )
                )
            ]

    def publish(self, run_id: str) -> None:
        with self.sessions.begin() as session:
            # Take the single writer lock before reads; avoids SQLite snapshot upgrade races.
            session.execute(
                update(CatalogueRunProfileRow)
                .where(CatalogueRunProfileRow.run_id == run_id)
                .values(source_id=CatalogueRunProfileRow.source_id)
            )
            run = session.get(ScrapeRunRow, run_id)
            if run is None or run.status != "success" or run.finished_at is None:
                raise ValueError("only a complete successful run may replace the catalogue")
            metadata = session.get(CatalogueRunProfileRow, run_id)
            fingerprint = metadata.profile_fingerprint if metadata else "legacy"
            if fingerprint != "legacy":
                assert metadata is not None
                coverage = Coverage.model_validate(metadata.coverage)
                coverage.require_publishable()
                profile = AcquisitionProfile.model_validate(metadata.profile)
                if profile.fingerprint != fingerprint or profile.source_id != run.source_id:
                    raise ValueError("recorded profile identity is invalid")
            head = session.get(CatalogueProfileHeadRow, (run.source_id, fingerprint))
            if head is not None and head.run_id == run_id:
                return
            if head is not None:
                previous = session.get(ScrapeRunRow, head.run_id)
                if previous is not None and previous.started_at > run.started_at:
                    raise ValueError("an older run cannot replace a newer published catalogue")
            batch = self.reader.get_batch(run_id, run.source_id)
            rows: list[dict[str, Any]] = []
            count = 0
            for observed in self.reader.iter_offers(batch):
                if observed.observation_id is None:
                    raise ValueError("catalogue publication needs persisted observation identities")
                offer = observed.offer
                rows.append(
                    {
                        "id": str(
                            uuid5(
                                NAMESPACE_URL,
                                f"grocery-catalogue/{run_id}/{observed.observation_id}",
                            )
                        ),
                        "run_id": run_id,
                        "observation_id": observed.observation_id,
                        "scope": offer.scope,
                        "retailer_id": offer.product.store_id,
                        "normalized_name": offer.product.normalized_name,
                        "category": offer.product.category,
                        "unit": offer.unit_price.unit.value,
                        "currency": offer.currency.value,
                        "unit_price_units": int(offer.unit_price.amount * 10000),
                        "requires_loyalty": bool(
                            offer.promotion and offer.promotion.requires_loyalty
                        ),
                        "price_qualifier": offer.price_qualifier.value,
                        "availability": offer.availability.value,
                        "observed_at": observed.observed_at,
                        "valid_from": offer.valid_from,
                        "valid_until": offer.valid_until,
                    }
                )
                count += 1
                if len(rows) == 200:
                    session.execute(insert(CatalogueEntryRow), rows)
                    rows.clear()
            if not count:
                raise ValueError("a zero-item run cannot replace a published catalogue")
            if rows:
                session.execute(insert(CatalogueEntryRow), rows)
            if head is None:
                session.add(
                    CatalogueProfileHeadRow(
                        source_id=run.source_id,
                        profile_fingerprint=fingerprint,
                        run_id=run_id,
                        published_at=utc_now(),
                    )
                )
            else:
                head.run_id, head.published_at = run_id, utc_now()
            if metadata is None:
                session.add(
                    CatalogueRunProfileRow(
                        run_id=run_id,
                        source_id=run.source_id,
                        profile_fingerprint="legacy",
                        profile=None,
                        coverage={
                            "profile_fingerprint": "legacy",
                            "observed": 0,
                            "complete": False,
                            "actual_scopes": [],
                            "warnings": ["Acquisition coverage is unknown."],
                        },
                    )
                )
            if fingerprint == "legacy":
                legacy = session.get(CatalogueHeadRow, run.source_id)
                if legacy is None:
                    session.add(
                        CatalogueHeadRow(
                            source_id=run.source_id, run_id=run_id, published_at=utc_now()
                        )
                    )
                else:
                    legacy.run_id, legacy.published_at = run_id, utc_now()
            session.flush()
            # These derived rows are disposable; observations, runs and raw evidence are retained.
            retained = select(CatalogueProfileHeadRow.run_id).union(select(CatalogueHeadRow.run_id))
            session.execute(
                delete(CatalogueEntryRow).where(CatalogueEntryRow.run_id.not_in(retained))
            )

    def _state(
        self,
        session: Session,
        source_id: str,
        now: datetime,
        profile_fingerprint: str | None = None,
    ) -> CatalogueState:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("catalogue reads require an aware timestamp")
        fingerprint = profile_fingerprint or "legacy"
        head = session.get(CatalogueProfileHeadRow, (source_id, fingerprint))
        if head is None:
            raise CatalogueNotReady("catalogue is empty; collect or rebuild a complete batch first")
        run = session.get(ScrapeRunRow, head.run_id)
        if run is None or run.status != "success" or run.finished_at is None:
            raise ValueError("published catalogue does not reference a complete run")
        expires = run.finished_at + timedelta(hours=self.settings.max_age_hours)
        if not run.finished_at <= now <= expires:
            raise ValueError("published catalogue is stale or in the future; rescan required")
        metadata = session.get(CatalogueRunProfileRow, run.id)
        coverage = (
            Coverage.model_validate(metadata.coverage)
            if metadata
            else Coverage(
                profile_fingerprint="legacy",
                complete=False,
                warnings=("Acquisition coverage is unknown.",),
            )
        )
        if fingerprint != "legacy":
            coverage.require_publishable()
        latest = session.scalar(
            select(ScrapeRunRow)
            .join(CatalogueRunProfileRow, CatalogueRunProfileRow.run_id == ScrapeRunRow.id)
            .where(
                ScrapeRunRow.source_id == source_id,
                CatalogueRunProfileRow.profile_fingerprint == fingerprint,
            )
            .order_by(ScrapeRunRow.started_at.desc(), ScrapeRunRow.id)
            .limit(1)
        )
        assert latest is not None
        latest_metadata = session.get(CatalogueRunProfileRow, latest.id)
        latest_complete = fingerprint == "legacy" or bool(
            latest_metadata and latest_metadata.coverage.get("complete")
        )
        degraded = (
            latest.status != "success"
            or not latest_complete
            or (latest.id != run.id and latest.started_at > run.started_at)
        )
        if degraded and not self.settings.allow_cached_on_failure:
            raise ValueError(
                "latest source run is incomplete or unsuccessful; cached reads disabled"
            )
        warnings = (
            (
                f"Latest rescan is {latest.status}; using complete cached run {run.id} "
                f"from {run.finished_at.isoformat()} within its freshness limit.",
            )
            if degraded
            else ()
        ) + coverage.warnings
        if fingerprint == "legacy" and not coverage.warnings:
            warnings += (
                "Legacy collection has unknown acquisition coverage; no profile is inferred.",
            )
        return CatalogueState(
            source_id=source_id,
            run_id=run.id,
            batch_finished_at=run.finished_at,
            published_at=head.published_at,
            fresh_until=expires,
            latest_run_id=latest.id,
            latest_run_status=latest.status,
            degraded=degraded,
            warnings=warnings,
            profile_fingerprint=fingerprint,
            coverage_complete=coverage.complete,
            actual_scope=coverage.actual_scope,
            actual_scopes=tuple(getattr(coverage, "actual_scopes", ())),
        )

    def state(
        self, source_id: str, now: datetime, profile_fingerprint: str | None = None
    ) -> CatalogueState:
        with self.sessions() as session:
            return self._state(session, source_id, now, profile_fingerprint)

    def search(self, query: CatalogueQuery, now: datetime) -> CataloguePage:
        with self.sessions() as session:
            state = self._state(session, query.source_id, now, query.profile_fingerprint)
            today = now.astimezone(ZoneInfo(self.settings.timezone)).date()
            cutoff = now - timedelta(hours=self.settings.max_age_hours)
            predicates = [
                CatalogueEntryRow.run_id == state.run_id,
                CatalogueEntryRow.currency == query.currency.value,
                CatalogueEntryRow.observed_at >= cutoff,
                CatalogueEntryRow.observed_at <= now,
                or_(CatalogueEntryRow.valid_from.is_(None), CatalogueEntryRow.valid_from <= today),
                or_(
                    CatalogueEntryRow.valid_until.is_(None), CatalogueEntryRow.valid_until >= today
                ),
            ]
            if query.scope is not None:
                predicates.append(CatalogueEntryRow.scope == query.scope)
            if query.category is not None:
                predicates.append(CatalogueEntryRow.category == query.category)
            if query.retailers:
                predicates.append(CatalogueEntryRow.retailer_id.in_(query.retailers))
            if query.unit is not None:
                predicates.append(CatalogueEntryRow.unit == query.unit.value)
            if query.max_unit_price is not None:
                predicates.append(
                    CatalogueEntryRow.unit_price_units <= int(query.max_unit_price * 10000)
                )
            if not query.allow_loyalty:
                predicates.append(CatalogueEntryRow.requires_loyalty.is_(False))
            if not query.include_from:
                predicates.append(CatalogueEntryRow.price_qualifier == "exact")
            if not query.include_unavailable:
                predicates.append(CatalogueEntryRow.availability != "unavailable")
            if query.search:
                term = (
                    normalize_name(query.search)
                    .replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
                )
                predicates.append(CatalogueEntryRow.normalized_name.like(f"%{term}%", escape="\\"))
                if not term:
                    predicates.append(false())
            total = session.scalar(
                select(func.count()).select_from(CatalogueEntryRow).where(*predicates)
            )
            sort = (
                (
                    CatalogueEntryRow.unit_price_units,
                    CatalogueEntryRow.normalized_name,
                    CatalogueEntryRow.id,
                )
                if query.sort == "unit_price"
                else (CatalogueEntryRow.normalized_name, CatalogueEntryRow.id)
            )
            statement = (
                select(ObservationRow.payload, CatalogueEntryRow.observed_at)
                .join(CatalogueEntryRow, CatalogueEntryRow.observation_id == ObservationRow.id)
                .where(*predicates)
                .order_by(*sort)
                .offset(query.offset)
                .limit(query.limit)
            )
            items = tuple(
                CatalogueItem(offer=Offer.model_validate(payload), observed_at=at)
                for payload, at in session.execute(statement)
            )
            return CataloguePage(
                generated_at=now,
                state=state,
                total=total or 0,
                offset=query.offset,
                limit=query.limit,
                items=items,
            )


class PublishedOfferReader:
    """Supply a pinned, visibly degraded cache batch to the existing meal planner."""

    def __init__(
        self,
        reader: BatchOfferReader,
        catalogue: SQLAlchemyCatalogueRepository,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.reader, self.catalogue, self.clock = reader, catalogue, clock

    def latest_batch(self, source_id: str) -> OfferBatch:
        try:
            state = self.catalogue.state(source_id, self.clock())
        except CatalogueNotReady:
            return self.reader.latest_batch(source_id)
        return OfferBatch(
            state.run_id,
            state.source_id,
            state.batch_finished_at,
            state.warnings,
            state.latest_run_status,
        )

    def get_batch(self, run_id: str, source_id: str) -> OfferBatch:
        return self.reader.get_batch(run_id, source_id)

    def iter_offers(self, batch: OfferBatch) -> Iterator[ObservedOffer]:
        return self.reader.iter_offers(batch)
