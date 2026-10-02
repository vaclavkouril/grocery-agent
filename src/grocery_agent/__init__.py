"""Retailer-independent grocery acquisition foundation."""

__version__ = "0.1.0"

from grocery_agent.catalogue.models import CatalogueSnapshot
from grocery_agent.recipe_config import RecipeSettings
from grocery_agent.recipe_service import RecipeService
from grocery_agent.recipes import RecipeRequest, RecipeResult

__all__ = [
    "CatalogueSnapshot",
    "RecipeRequest",
    "RecipeResult",
    "RecipeService",
    "RecipeSettings",
    "__version__",
]
