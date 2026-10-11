"""Resolve optional account shopping defaults without replacing explicit choices."""

from typing import Any

from grocery_agent.recipes import RecipeRequest


def apply_shopping_preferences(request: RecipeRequest, settings: dict[str, Any]) -> RecipeRequest:
    overrides = {
        field: settings[field]
        for field in ("retailer_ids", "max_stores")
        if field not in request.model_fields_set and settings.get(field) is not None
    }
    if "retailer_ids" in overrides:
        overrides["retailer_ids"] = tuple(overrides["retailer_ids"])
    return request.model_copy(update=overrides) if overrides else request
