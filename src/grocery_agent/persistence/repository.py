import hashlib
import json
from decimal import Decimal
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from grocery_agent.models.observation import PriceObservation
from grocery_agent.models.offer import Offer
from grocery_agent.models.product import Product
from grocery_agent.persistence.base import StoredSnapshot
from grocery_agent.persistence.schema import (
    ObservationRow,
    OfferRow,
    ProductRow,
    ScrapeItemRow,
    ScrapeRunRow,
    SnapshotRow,
    StoreProductRow,
)
from grocery_agent.pipeline.results import ScrapeResult

MONEY_SCALE = Decimal(10000)


def stable_id(*parts: str) -> str:
    return str(uuid5(NAMESPACE_URL, json.dumps(parts, ensure_ascii=False)))


def state_fingerprint(offer: Offer) -> str:
    payload = offer.model_dump(mode="json", exclude={"source_url"}, exclude_computed_fields=True)
    # Preserve fingerprints of historical observations without purchase metadata.
    if payload.get("purchase_terms") is None:
        payload.pop("purchase_terms", None)
    # Quantities are semantically equivalent across g/kg and ml/l.
    terms = payload.get("purchase_terms") or {}
    for quantity in (
        payload["price_basis"],
        payload["product"].get("quantity"),
        terms.get("minimum_quantity"),
        terms.get("quantity_increment"),
    ):
        if quantity and quantity["unit"] in {"g", "ml"}:
            quantity["amount"] = format((Decimal(quantity["amount"]) / 1000).normalize(), "f")
            quantity["unit"] = {"g": "kg", "ml": "l"}[quantity["unit"]]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


class SQLAlchemyOfferRepository:
    def __init__(self, engine: Engine) -> None:
        self.sessions = sessionmaker(engine, expire_on_commit=False)

    def start_run(self, result: ScrapeResult) -> None:
        with self.sessions.begin() as session:
            session.add(
                ScrapeRunRow(
                    id=result.run_id,
                    source_id=result.source_id,
                    started_at=result.started_at,
                    status=result.status,
                )
            )

    def finish_run(self, result: ScrapeResult) -> None:
        with self.sessions.begin() as session:
            row = session.get(ScrapeRunRow, result.run_id)
            if row is None:
                raise ValueError("unknown run")
            for key in (
                "finished_at",
                "status",
                "fetched",
                "accepted",
                "rejected",
                "changed",
                "errors",
                "error_details",
            ):
                setattr(row, key, getattr(result, key))

    def add_snapshot(self, run_id: str, snapshot: StoredSnapshot) -> None:
        source = snapshot.evidence
        with self.sessions.begin() as session:
            session.add(
                SnapshotRow(
                    id=snapshot.id,
                    run_id=run_id,
                    sha256=snapshot.sha256,
                    relative_path=snapshot.relative_path,
                    url=source.url,
                    media_type=source.media_type,
                    fetched_at=source.fetched_at,
                    locator=source.locator,
                    source_metadata=source.metadata,
                )
            )

    def accept(self, run_id: str, observation: PriceObservation, product: Product | None) -> bool:
        offer = observation.offer
        sku_id = stable_id("sku", offer.product.store_id, offer.product.sku)
        offer_id = stable_id("offer", sku_id, offer.offer_key, offer.scope)
        fingerprint = state_fingerprint(offer)
        with self.sessions.begin() as session:
            self._upsert_product(session, observation, product, sku_id)
            row = session.get(OfferRow, offer_id)
            if row is None:
                row = OfferRow(
                    id=offer_id,
                    store_product_id=sku_id,
                    offer_key=offer.offer_key,
                    scope=offer.scope,
                    version=0,
                )
                session.add(row)
                session.flush()
            latest = session.scalar(
                select(ObservationRow)
                .where(ObservationRow.offer_id == offer_id)
                .order_by(ObservationRow.version.desc())
                .limit(1)
            )
            if latest is not None and observation.observed_at < latest.last_seen_at:
                raise ValueError("out-of-order observation would corrupt current offer state")
            changed = latest is None or latest.fingerprint != fingerprint
            if changed:
                row.version += 1
                latest = ObservationRow(
                    id=str(uuid4()),
                    offer_id=offer_id,
                    version=row.version,
                    fingerprint=fingerprint,
                    current_price_units=int(offer.current_price * MONEY_SCALE),
                    regular_price_units=(
                        int(offer.regular_price * MONEY_SCALE)
                        if offer.regular_price is not None
                        else None
                    ),
                    currency=offer.currency,
                    payload=offer.model_dump(mode="json", exclude_computed_fields=True),
                    first_seen_at=observation.observed_at,
                    last_seen_at=observation.observed_at,
                    snapshot_id=observation.snapshot_id,
                )
                session.add(latest)
                session.flush()
            else:
                assert latest is not None
                latest.last_seen_at = observation.observed_at
            session.add(
                ScrapeItemRow(
                    id=str(uuid4()),
                    run_id=run_id,
                    snapshot_id=observation.snapshot_id,
                    observation_id=latest.id,
                    status="changed" if changed else "unchanged",
                )
            )
            return changed

    @staticmethod
    def _upsert_product(
        session: Session, observation: PriceObservation, product: Product | None, sku_id: str
    ) -> None:
        product_id = str(product.id) if product is not None else None
        if product is not None and session.get(ProductRow, product_id) is None:
            session.add(
                ProductRow(
                    id=product_id,
                    gtin=product.gtin,
                    details=product.model_dump(mode="json", exclude_computed_fields=True),
                )
            )
            session.flush()
        sku = observation.offer.product
        row = session.get(StoreProductRow, sku_id)
        if row is None:
            row = StoreProductRow(
                id=sku_id,
                store_id=sku.store_id,
                sku=sku.sku,
                product_id=product_id,
                details=sku.model_dump(mode="json", exclude_computed_fields=True),
            )
            session.add(row)
        else:
            # Do not guess a match if today's source omits GTIN; preserve an existing resolved link.
            if product_id is not None:
                row.product_id = product_id
            row.details = sku.model_dump(mode="json", exclude_computed_fields=True)
        session.flush()

    def reject(self, run_id: str, snapshot_id: str, details: str) -> None:
        with self.sessions.begin() as session:
            session.add(
                ScrapeItemRow(
                    id=str(uuid4()),
                    run_id=run_id,
                    snapshot_id=snapshot_id,
                    status="rejected",
                    details=details,
                )
            )

    def recent_runs(self, limit: int) -> list[dict[str, Any]]:
        with self.sessions() as session:
            rows = session.scalars(
                select(ScrapeRunRow).order_by(ScrapeRunRow.started_at.desc()).limit(limit)
            )
            return [
                {
                    "run_id": row.id,
                    "source_id": row.source_id,
                    "status": row.status,
                    "started_at": row.started_at.isoformat(),
                    "finished_at": row.finished_at.isoformat() if row.finished_at else None,
                    "fetched": row.fetched,
                    "accepted": row.accepted,
                    "rejected": row.rejected,
                    "changed": row.changed,
                    "errors": row.errors,
                    "error_details": row.error_details,
                }
                for row in rows
            ]
