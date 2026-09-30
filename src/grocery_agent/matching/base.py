from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from grocery_agent.models.product import Product, StoreProduct


class ProductResolver(Protocol):
    def resolve(self, product: StoreProduct) -> Product | None: ...


class ExactGTINResolver:
    """Only exact validated GTIN identity. No name matching or weight equivalence guesses."""

    def resolve(self, product: StoreProduct) -> Product | None:
        if product.gtin is None:
            return None
        return Product(
            id=uuid5(NAMESPACE_URL, f"urn:gtin:{product.gtin}"),
            name=product.name,
            brand=product.brand,
            category=product.category,
            quantity=product.quantity,
            gtin=product.gtin,
            variable_weight=product.variable_weight,
        )
