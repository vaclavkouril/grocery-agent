import asyncio
import logging

from pydantic import ValidationError

from grocery_agent.matching.base import ProductResolver
from grocery_agent.models.common import utc_now
from grocery_agent.models.observation import PriceObservation
from grocery_agent.models.offer import Offer
from grocery_agent.persistence.base import OfferRepository, SnapshotStore
from grocery_agent.pipeline.results import ScrapeResult
from grocery_agent.stores.base import AcquisitionAdapter, AdapterContext

logger = logging.getLogger(__name__)


class ScrapePipeline:
    def __init__(
        self, repository: OfferRepository, snapshots: SnapshotStore, resolver: ProductResolver
    ) -> None:
        self.repository = repository
        self.snapshots = snapshots
        self.resolver = resolver

    async def run(self, adapter: AcquisitionAdapter, context: AdapterContext) -> ScrapeResult:
        result = ScrapeResult(source_id=adapter.source_id)
        self.repository.start_run(result)
        logger.info("scrape_started", extra={"fields": result.as_dict()})
        try:
            async for item in adapter.fetch_offers(context):
                result.fetched += 1
                snapshot = self.snapshots.save(item.evidence)
                self.repository.add_snapshot(result.run_id, snapshot)
                try:
                    if item.error is not None:
                        raise ValueError(item.error)
                    offer = Offer.model_validate(item.candidate)
                    adapter.validate_offer(offer)
                    observation = PriceObservation(
                        offer=offer, observed_at=item.evidence.fetched_at, snapshot_id=snapshot.id
                    )
                    product = self.resolver.resolve(offer.product)
                    changed = self.repository.accept(result.run_id, observation, product)
                except (ValidationError, ValueError) as exc:
                    # Validation details exclude input values: raw evidence is the debugging source.
                    details = (
                        str(exc.errors(include_input=False, include_url=False))
                        if isinstance(exc, ValidationError)
                        else str(exc)
                    )
                    self.repository.reject(result.run_id, snapshot.id, details)
                    result.rejected += 1
                    logger.warning(
                        "item_rejected",
                        extra={
                            "fields": {
                                "run_id": result.run_id,
                                "source_id": result.source_id,
                                "snapshot_id": snapshot.id,
                                "locator": item.evidence.locator,
                                "error": details,
                            }
                        },
                    )
                    continue
                result.accepted += 1
                result.changed += int(changed)
            result.status = "partial" if result.rejected else "success"
            if result.fetched == 0:
                result.status = "empty"
                result.errors += 1
                result.error_details.append("adapter returned zero items; coverage requires review")
        except asyncio.CancelledError:
            result.status = "cancelled"
            result.errors += 1
            result.error_details.append("scrape cancelled")
            raise
        except Exception as exc:
            result.status = "failed"
            result.errors += 1
            result.error_details.append(f"{type(exc).__name__}: {exc}")
            logger.exception(
                "scrape_failed",
                extra={
                    "fields": {
                        "run_id": result.run_id,
                        "source_id": result.source_id,
                    }
                },
            )
        finally:
            result.finished_at = utc_now()
            self.repository.finish_run(result)
            logger.info("scrape_finished", extra={"fields": result.as_dict()})
        return result
