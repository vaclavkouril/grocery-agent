import re
from collections import Counter
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from itertools import combinations
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime

from grocery_agent.meals.catalog import Ingredient, MealCatalog, MealPolicy, Nutrients
from grocery_agent.models.common import DomainModel
from grocery_agent.models.offer import Availability, Currency, Offer, PriceQualifier, PromotionType
from grocery_agent.models.product import Unit
from grocery_agent.persistence.reader import CurrentOfferReader, OfferBatch


class IngredientPrice(DomainModel):
    ingredient_id: str
    label: str
    price_per_kg_czk: Decimal
    offer: Offer | None = None
    observed_at: AwareDatetime | None = None


class MealLine(DomainModel):
    price: IngredientPrice
    purchased_grams: Decimal
    edible_grams: Decimal
    usage_cost_czk: Decimal


class PlannedMeal(DomainModel):
    title: str
    minutes: int
    servings: int
    lines: tuple[MealLine, ...]
    nutrients_per_serving: Nutrients
    usage_cost_per_serving_czk: Decimal
    protein_g_per_czk: Decimal
    stores: tuple[str, ...]
    steps: tuple[str, ...]


class MealReport(DomainModel):
    generated_at: AwareDatetime
    batch_finished_at: AwareDatetime
    run_id: str
    policy: MealPolicy
    meals: tuple[PlannedMeal, ...]
    cheap_ingredients: tuple[IngredientPrice, ...]
    rejected_offers: dict[str, int]


def retailer(price: IngredientPrice) -> str:
    return price.offer.product.store_id if price.offer else ""


def rejection_reason(offer: Offer, policy: MealPolicy) -> str | None:
    if offer.scope != policy.scope:
        return "scope"
    if policy.retailers and offer.product.store_id not in policy.retailers:
        return "retailer"
    if offer.currency != Currency.CZK:
        return "currency"
    if offer.availability == Availability.UNAVAILABLE:
        return "unavailable"
    if offer.price_qualifier != PriceQualifier.EXACT or offer.current_price <= 0:
        return "price"
    promotion = offer.promotion
    if promotion:
        if promotion.requires_loyalty and not policy.allow_loyalty:
            return "loyalty"
        if promotion.minimum_purchase > 1 or promotion.kind in {
            PromotionType.MULTIBUY,
            PromotionType.COUPON,
            PromotionType.OTHER,
        }:
            return "conditional"
        # Preserve terms, but don't turn ambiguous bundles/ranges into a guaranteed recipe price.
        terms = promotion.conditions or ""
        if re.search(r"\d+\s*\+\s*\d+|zdarma|\d+\s*[-–]\s*\d+\s*(?:g|kg)\b", terms):
            return "conditional"
    if offer.unit_price.unit != Unit.KG:
        return "unknown_mass"
    return None


def matches(ingredient: Ingredient, offer: Offer) -> bool:
    name = offer.product.name.casefold()
    terms = (offer.promotion.conditions or "").casefold() if offer.promotion else ""
    return bool(
        ingredient.name_pattern
        and re.search(ingredient.name_pattern, name)
        and not re.search(ingredient.exclude_pattern, f"{name} {terms}")
    )


def plan_meals(reader: CurrentOfferReader, catalog: MealCatalog, now: datetime) -> MealReport:
    if now.tzinfo is None:
        raise ValueError("planning requires an aware timestamp")
    policy = catalog.policy
    batch: OfferBatch = reader.latest_batch(policy.source_id)
    max_age = timedelta(hours=policy.max_age_hours)
    if not timedelta(0) <= now - batch.finished_at <= max_age:
        raise ValueError("latest batch is stale or in the future")
    today = now.astimezone(ZoneInfo(policy.timezone)).date()
    rejected: Counter[str] = Counter()
    best: dict[str, IngredientPrice] = {}
    by_store: dict[tuple[str, str], IngredientPrice] = {}
    for key, ingredient in catalog.ingredients.items():
        if ingredient.pantry_price_per_kg_czk is not None:
            best[key] = IngredientPrice(
                ingredient_id=key,
                label=ingredient.label,
                price_per_kg_czk=ingredient.pantry_price_per_kg_czk,
            )
    for observed in reader.iter_offers(batch):
        offer = observed.offer
        reason = rejection_reason(offer, policy)
        if not timedelta(0) <= now - observed.observed_at <= max_age:
            reason = "stale"
        elif (offer.valid_from and offer.valid_from > today) or (
            offer.valid_until and offer.valid_until < today
        ):
            reason = "validity"
        if reason:
            rejected[reason] += 1
            continue
        for key, ingredient in catalog.ingredients.items():
            if policy.lactose_free and not ingredient.lactose_free:
                continue
            if matches(ingredient, offer):
                candidate = IngredientPrice(
                    ingredient_id=key,
                    label=ingredient.label,
                    price_per_kg_czk=offer.unit_price.amount,
                    offer=offer,
                    observed_at=observed.observed_at,
                )
                # Stable tie-break, independent of pagination order.
                previous = best.get(key)
                store_key = (key, offer.product.store_id)
                store_previous = by_store.get(store_key)
                if store_previous is None or (candidate.price_per_kg_czk, offer.product.sku) < (
                    store_previous.price_per_kg_czk,
                    store_previous.offer.product.sku if store_previous.offer else "",
                ):
                    by_store[store_key] = candidate
                if previous is None or (
                    candidate.price_per_kg_czk,
                    offer.product.store_id,
                    offer.product.sku,
                ) < (
                    previous.price_per_kg_czk,
                    previous.offer.product.store_id if previous.offer else "",
                    previous.offer.product.sku if previous.offer else "",
                ):
                    best[key] = candidate
    meals: list[PlannedMeal] = []
    for recipe in catalog.recipes:
        if not recipe.grams.keys() <= best.keys():
            continue
        if policy.lactose_free and any(
            not catalog.ingredients[key].lactose_free for key in recipe.grams
        ):
            continue
        relevant_stores = sorted({store for key, store in by_store if key in recipe.grams})
        basket_options: list[dict[str, IngredientPrice]] = []
        for count in range(1, min(policy.max_stores, len(relevant_stores)) + 1):
            for subset in combinations(relevant_stores, count):
                basket = {key: price for key, price in best.items() if price.offer is None}
                for key in recipe.grams:
                    options = [
                        by_store[(key, store)] for store in subset if (key, store) in by_store
                    ]
                    if options:
                        basket[key] = min(
                            options,
                            key=lambda price: (
                                price.price_per_kg_czk,
                                price.offer.product.store_id if price.offer else "",
                            ),
                        )
                if recipe.grams.keys() <= basket.keys():
                    basket_options.append(basket)
        if not basket_options:
            continue
        basket = min(
            basket_options,
            key=lambda option: (
                sum(recipe.grams[key] * option[key].price_per_kg_czk for key in recipe.grams),
                len(
                    {
                        price.offer.product.store_id
                        for key, price in option.items()
                        if key in recipe.grams and price.offer
                    }
                ),
                tuple(retailer(option[key]) for key in recipe.grams),
            ),
        )
        nutrients = Nutrients(kcal="0", protein_g="0", carbs_g="0", fat_g="0")
        cost = policy.seasoning_allowance_czk
        lines: list[MealLine] = []
        stores: set[str] = set()
        for key, grams in recipe.grams.items():
            ingredient, price = catalog.ingredients[key], basket[key]
            edible = grams * ingredient.edible_fraction
            nutrients = nutrients.plus(ingredient.nutrients_per_100g_edible.scaled(edible / 100))
            usage = grams / 1000 * price.price_per_kg_czk
            cost += usage
            if price.offer:
                stores.add(price.offer.product.store_id)
            lines.append(
                MealLine(
                    price=price,
                    purchased_grams=grams * policy.servings,
                    edible_grams=edible * policy.servings,
                    usage_cost_czk=(usage * policy.servings).quantize(
                        Decimal("0.01"), ROUND_HALF_UP
                    ),
                )
            )
        if (
            nutrients.protein_g < policy.min_protein_g
            or cost <= 0
            or nutrients.kcal > policy.max_kcal
            or cost > policy.max_cost_per_serving_czk
        ):
            continue
        meals.append(
            PlannedMeal(
                title=recipe.title,
                minutes=recipe.minutes,
                servings=policy.servings,
                lines=tuple(lines),
                nutrients_per_serving=nutrients,
                usage_cost_per_serving_czk=cost.quantize(Decimal("0.01"), ROUND_HALF_UP),
                protein_g_per_czk=(nutrients.protein_g / cost).quantize(Decimal("0.001")),
                stores=tuple(sorted(stores)),
                steps=recipe.steps,
            )
        )
    if not meals:
        raise ValueError(
            "no complete recipe meets the configured protein, calorie, and cost limits"
        )
    meals.sort(
        key=lambda meal: (
            -(
                meal.protein_g_per_czk
                if policy.ranking == "protein_per_czk"
                else meal.nutrients_per_serving.protein_g
            ),
            -meal.nutrients_per_serving.protein_g,
            meal.usage_cost_per_serving_czk,
            meal.title,
        )
    )
    return MealReport(
        generated_at=now,
        batch_finished_at=batch.finished_at,
        run_id=batch.run_id,
        policy=policy,
        meals=tuple(meals),
        cheap_ingredients=tuple(value for value in best.values() if value.offer is not None),
        rejected_offers=dict(rejected),
    )
