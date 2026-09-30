from collections.abc import Callable

from grocery_agent.models.offer import Offer
from grocery_agent.stores.base import AcquisitionAdapter, AdapterContext


async def assert_adapter_contract(
    factory: Callable[[], AcquisitionAdapter], context: AdapterContext, *, expected_count: int
) -> None:
    """Reusable contract check with a fixture-backed production adapter factory."""
    adapter = factory()
    count = 0
    stream = adapter.fetch_offers(context)
    assert hasattr(stream, "__aiter__")
    async for item in stream:
        count += 1
        assert item.error is None, item.error
        assert item.candidate is not None
        evidence = item.evidence
        assert isinstance(evidence.content, bytes) and evidence.content
        assert evidence.locator and evidence.url and evidence.media_type
        assert evidence.fetched_at.utcoffset() is not None
        offer = Offer.model_validate(item.candidate)
        adapter.validate_offer(offer)
        assert offer.product.sku and offer.product.normalized_name
        assert offer.current_price >= 0
        assert offer.unit_price.amount >= 0
        assert offer.price_basis.amount > 0
        # Persistence consumes this canonical shape, without raw source fields.
        payload = offer.model_dump(mode="json", exclude_computed_fields=True)
        assert Offer.model_validate(payload) == offer
    assert count == expected_count
