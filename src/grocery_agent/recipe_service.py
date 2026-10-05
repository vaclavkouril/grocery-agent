"""Pin catalogue inputs, generate drafts, and evaluate with the Decimal meal planner."""

from collections.abc import Callable, Iterator, Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from grocery_agent.application.parameters import (
    MealOverrides,
    PantryOverrides,
    PolicyOverrides,
    parse_owned_stock,
    resolve_parameters,
)
from grocery_agent.config import Settings
from grocery_agent.localization import Language, translate
from grocery_agent.meals.catalog import MealCatalog, Recipe
from grocery_agent.meals.planner import matches, plan_meals, rejection_reason
from grocery_agent.meals.report import render_html
from grocery_agent.models.common import utc_now
from grocery_agent.models.offer import Offer
from grocery_agent.persistence.reader import ObservedOffer, OfferBatch
from grocery_agent.recipe_cache import RecipeCache, RecipeOptions
from grocery_agent.recipes import (
    CodexProvider,
    OllamaProvider,
    RecipeDraft,
    RecipeProvider,
    RecipeRequest,
)


def effective_catalog(
    catalog: MealCatalog,
    request: RecipeRequest,
    *,
    source_scopes: Mapping[str, tuple[str, ...]] | None = None,
) -> MealCatalog:
    allowed = source_scopes or {catalog.policy.source_id: (catalog.policy.scope,)}
    if set(request.source_ids) - allowed.keys():
        raise ValueError("this configuration supports only explicitly permitted recipe sources")
    if set(request.exclusions) - catalog.ingredients.keys():
        raise ValueError("exclusions must be known ingredient IDs")
    if catalog.policy.retailers and set(request.retailer_ids) - set(catalog.policy.retailers):
        raise ValueError("retailer_ids must be a subset of the configured retailer allowlist")
    if set(request.use_first) & set(request.exclusions):
        raise ValueError("use_first ingredients must not be excluded")
    policy: dict[str, Any] = {
        "servings": request.servings,
        "meal_style": request.meal_style,
        "max_stores": request.max_stores,
    }
    if request.max_cost_per_serving_czk is not None:
        policy["max_cost_per_serving_czk"] = request.max_cost_per_serving_czk
    for field in ("allow_loyalty", "min_protein_g", "max_kcal", "max_minutes"):
        value = getattr(request, field)
        if value is not None:
            policy[field] = value
    if request.retailer_ids:
        policy["retailers"] = request.retailer_ids
    overrides = MealOverrides(
        policy=PolicyOverrides.model_validate(policy),
        pantry=PantryOverrides(
            items=parse_owned_stock([f"{key}={value}" for key, value in request.pantry.items()]),
            use_first=request.use_first,
            seasonings_available=request.seasonings_available,
        ),
    )
    resolved = resolve_parameters(catalog, overrides).apply_to(catalog)
    # An excluded owned ingredient must not remain usable by generated drafts.
    payload = resolved.model_dump(mode="json")
    payload["policy"]["source_id"] = request.source_ids[0]
    for key in request.exclusions:
        payload["ingredients"].pop(key, None)
        payload["pantry"]["items"].pop(key, None)
    payload["recipes"] = [
        recipe
        for recipe in payload["recipes"]
        if not set(recipe["grams"]) & set(request.exclusions)
    ]
    if not payload["recipes"]:
        # A generated draft may still use remaining known ingredients. Preserve one
        # placeholder only for catalogue validation; it is replaced before evaluation.
        remaining = list(payload["ingredients"])
        if not remaining:
            raise ValueError("exclusions leave no known ingredients")
        payload["recipes"] = [
            {
                "title": "Pending generated recipe",
                "minutes": 1,
                "grams": {remaining[0]: "1"},
                "steps": ["Pending"],
            }
        ]
    return MealCatalog.model_validate(payload)


class SnapshotReader:
    def __init__(self, inputs: dict[str, Any]) -> None:
        self.batch = OfferBatch(
            inputs["run_id"],
            inputs["source_id"],
            datetime.fromisoformat(inputs["finished_at"]),
            tuple(inputs["warnings"]),
            inputs["latest_run_status"],
        )
        self.offers = tuple(
            ObservedOffer(
                Offer.model_validate(item["offer"]),
                datetime.fromisoformat(item["observed_at"]),
                item["observation_id"],
                item.get("source_id", inputs["source_id"]),
                item.get("run_id", inputs["run_id"]),
            )
            for item in inputs["offers"]
        )

    def latest_batch(self, source_id: str) -> OfferBatch:
        return self.batch

    def iter_offers(self, batch: OfferBatch) -> Iterator[ObservedOffer]:
        return iter(self.offers)


class RecipeService:
    def __init__(
        self,
        settings: Settings,
        backend: RecipeOptions,
        clock: Callable[[], datetime] = utc_now,
        provider: RecipeProvider | None = None,
        refresh: Callable[..., Any] | None = None,
    ) -> None:
        self.settings, self.backend, self.clock, self.provider = settings, backend, clock, provider
        self.cache = RecipeCache(settings, backend, clock, refresh=refresh)

    def prepare(self, request: RecipeRequest) -> dict[str, Any]:
        if request.provider == "ollama" and not request.model:
            raise ValueError("Ollama requires an explicitly configured local model")
        base = MealCatalog.load(self.settings.meal_config)
        catalog = effective_catalog(base, request, source_scopes=self.cache.scopes(request, base))
        # Validate request parameters before acquisition; scope compatibility uses the
        # original configured policy, not the request's effective primary source.
        inputs = self.cache.pin(request, base)
        reader = SnapshotReader(inputs)
        now = self.clock()
        today = now.astimezone(ZoneInfo(catalog.policy.timezone)).date()
        max_age = timedelta(hours=catalog.policy.max_age_hours)
        offers = [
            item
            for item in reader.offers
            if rejection_reason(
                item.offer,
                catalog.policy,
                allowed_scopes=inputs["source_scopes"].get(item.source_id, ()),
            )
            is None
            and timedelta(0) <= now - item.observed_at <= max_age
            and (item.offer.valid_from is None or item.offer.valid_from <= today)
            and (item.offer.valid_until is None or item.offer.valid_until >= today)
            and any(matches(ingredient, item.offer) for ingredient in catalog.ingredients.values())
        ]
        available = {
            key
            for key, ingredient in catalog.ingredients.items()
            if ingredient.pantry_price_per_kg_czk is not None
            or key in catalog.pantry.items
            or any(matches(ingredient, item.offer) for item in offers)
        }
        if catalog.policy.lactose_free:
            available = {key for key in available if catalog.ingredients[key].lactose_free}
        # Prioritize owned use-first stock without expanding the bounded context.
        selected = sorted(
            available,
            key=lambda key: (
                not (key in catalog.pantry.items and catalog.pantry.items[key].use_first),
                key,
            ),
        )[: self.backend.context_ingredients]
        context = {key: catalog.ingredients[key].model_dump(mode="json") for key in selected}
        inputs["catalog"] = catalog.model_dump(mode="json")
        inputs["ingredient_context"] = context
        inputs["prompt_version"] = "recipe-draft-v2"
        inputs["language"] = request.language
        inputs["effective_request"] = request.model_dump(mode="json")
        inputs["offers"] = [
            {
                "offer": item.offer.model_dump(mode="json", exclude_computed_fields=True),
                "observed_at": item.observed_at.isoformat(),
                "observation_id": item.observation_id,
                "source_id": item.source_id,
                "run_id": item.run_id,
            }
            for item in offers
        ]
        return inputs

    def run(self, request: RecipeRequest) -> tuple[dict[str, Any], str]:
        return self.execute(request, self.prepare(request))

    def execute(self, request: RecipeRequest, inputs: dict[str, Any]) -> tuple[dict[str, Any], str]:
        language: Language = inputs.get("language", request.language)
        if language not in {"cs", "en"}:
            raise ValueError("pinned recipe language must be cs or en")
        if "language" in inputs and language != request.language:
            raise ValueError("pinned recipe language does not match request")
        fingerprint = inputs.get("profile_fingerprint", "legacy")
        expected = request.profile_fingerprints or (
            {request.source_ids[0]: request.profile_fingerprint}
            if request.profile_fingerprint
            else {}
        )
        selected = inputs.get("profile_fingerprints", {inputs["source_id"]: fingerprint})
        if set(selected) != set(request.source_ids) or any(
            selected.get(source) != value for source, value in expected.items()
        ):
            raise ValueError("pinned recipe profile does not match request")
        metadata = {
            "run_id": inputs["run_id"],
            "source_id": inputs["source_id"],
            "profile_fingerprint": fingerprint,
            "actual_scope": inputs.get("actual_scope"),
            "actual_scopes": inputs.get("actual_scopes", []),
            "coverage_complete": inputs.get("coverage_complete", False),
            "warnings": list(inputs["warnings"]),
            "profile_fingerprints": selected,
            "catalogue_snapshot": inputs.get("catalogue_snapshot"),
            "collections": inputs.get("collections", []),
            "effective_request": inputs.get("effective_request", request.model_dump(mode="json")),
            "language": language,
        }
        catalog = MealCatalog.model_validate(inputs["catalog"])
        reader = SnapshotReader(inputs)
        if (
            request.provider == "template"
            and catalog.recipes[0].title == "Pending generated recipe"
        ):
            return {
                **metadata,
                "status": "no-feasible-recipe",
                "request_id": str(request.request_id),
                "run_id": reader.batch.run_id,
                "reason": "No configured template remains after exclusions.",
                "reason_code": "no-template-after-exclusions",
                "reason_localized": translate(
                    "No configured template remains after exclusions.", language
                ),
            }, no_feasible_html(language, "No configured template remains after exclusions.")
        if request.provider == "template":
            catalog = catalog.model_copy(
                update={"recipes": tuple(recipe.localized(language) for recipe in catalog.recipes)}
            )
        if request.provider != "template":
            provider = self.provider
            if provider is None:
                provider = (
                    OllamaProvider(
                        request.model or "", timeout=self.backend.provider_timeout_seconds
                    )
                    if request.provider == "ollama"
                    else CodexProvider(request.model, timeout=self.backend.provider_timeout_seconds)
                )
            import json

            context = json.dumps(inputs["ingredient_context"], ensure_ascii=False)
            for attempt in range(2):
                try:
                    draft = RecipeDraft.model_validate(provider.generate(request, context))
                    ids = [line.ingredient for line in draft.ingredients]
                    if len(set(ids)) != len(ids) or set(ids) - inputs["ingredient_context"].keys():
                        raise ValueError("use unique known eligible ingredient IDs")
                    recipe = Recipe(
                        title=draft.title,
                        meal_style=catalog.policy.meal_style,
                        minutes=draft.minutes,
                        steps=draft.steps,
                        grams={
                            line.ingredient: line.quantity / Decimal(request.servings)
                            for line in draft.ingredients
                        },
                    )
                    catalog = MealCatalog.model_validate(
                        {**catalog.model_dump(), "recipes": (recipe,)}
                    )
                    break
                except ValueError as exc:
                    if attempt:
                        raise ValueError(
                            "provider draft validation failed after one repair"
                        ) from exc
                    context += (
                        "\nRepair the draft: use unique known IDs, positive gram quantities, "
                        "steps and minutes."
                    )
        try:
            report = plan_meals(
                reader,
                catalog,
                self.clock(),
                batch=reader.batch,
                source_scopes=inputs.get("source_scopes"),
            )
        except ValueError as exc:
            if not str(exc).startswith("no complete recipe"):
                raise
            return {
                **metadata,
                "status": "no-feasible-recipe",
                "request_id": str(request.request_id),
                "run_id": reader.batch.run_id,
                "reason": str(exc),
                "reason_code": "no-complete-recipe-within-limits",
                "reason_localized": translate(
                    "No complete recipe meets the configured limits.", language
                ),
            }, no_feasible_html(language, "No complete recipe meets the configured limits.")
        return {
            **metadata,
            "status": "ok",
            "request_id": str(request.request_id),
            "provider": request.provider,
            "model": request.model,
            "prompt_version": inputs["prompt_version"],
            "report": report.model_dump(mode="json"),
        }, render_html(report, catalog, language=language)


def no_feasible_html(language: Language, reason: str) -> str:
    import html

    return (
        f'<!doctype html><html lang="{language}"><meta charset="utf-8">'
        f"<h1>{html.escape(translate('No feasible recipe.', language))}</h1>"
        f"<p>{html.escape(translate(reason, language))}</p></html>"
    )
