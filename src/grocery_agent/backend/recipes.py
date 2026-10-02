"""Compatibility imports; CLI and worker now use the shared recipe library."""

from grocery_agent.recipe_service import RecipeService, SnapshotReader, effective_catalog

RecipeExecutor = RecipeService

__all__ = ["RecipeExecutor", "SnapshotReader", "effective_catalog"]
