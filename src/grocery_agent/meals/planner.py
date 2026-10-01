import re
from collections import Counter
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from itertools import combinations
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime

from grocery_agent.meals.catalog import (
    Ingredient,
    MealCatalog,
    MealPolicy,
    MealStyle,
    Nutrients,
    Pantry,
)
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
    required_grams: Decimal
    owned_grams: Decimal
    purchased_grams: Decimal
    edible_grams: Decimal
    usage_cost_czk: Decimal


class PlannedMeal(DomainModel):
    title: str
    meal_style: MealStyle = "main"
    minutes: int
    servings: int
    lines: tuple[MealLine, ...]
    nutrients_per_serving: Nutrients
    usage_cost_per_serving_czk: Decimal
    # A free meal has no finite protein/price ratio; JSON must never contain infinity.
    protein_g_per_czk: Decimal | None
    use_first_grams: Decimal
    stores: tuple[str, ...]
    steps: tuple[str, ...]


class MealReport(DomainModel):
    generated_at: AwareDatetime
    batch_finished_at: AwareDatetime
    run_id: str
    policy: MealPolicy
    pantry: Pantry
    meals: tuple[PlannedMeal, ...]
    cheap_ingredients: tuple[IngredientPrice, ...]
    rejected_offers: dict[str, int]
    warnings: tuple[str, ...] = ()
    source_latest_run_status: str | None = None


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


def plan_meals(
    reader: CurrentOfferReader,
    catalog: MealCatalog,
    now: datetime,
    *,
    batch: OfferBatch | None = None,
) -> MealReport:
    if now.tzinfo is None:
        raise ValueError("planning requires an aware timestamp")
    policy = catalog.policy
    batch = batch or reader.latest_batch(policy.source_id)
    if batch.source_id != policy.source_id:
        raise ValueError("selected batch source does not match the meal policy")
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
        if recipe.meal_style != policy.meal_style:
            continue
        required = {key: grams * policy.servings for key, grams in recipe.grams.items()}
        owned = {key: catalog.pantry.available_grams(key, grams) for key, grams in required.items()}
        to_buy = {key: grams - owned[key] for key, grams in required.items()}
        if policy.lactose_free and any(
            not catalog.ingredients[key].lactose_free for key in recipe.grams
        ):
            continue
        relevant_stores = sorted(
            {store for key, store in by_store if key in recipe.grams and to_buy[key] > 0}
        )
        base = {key: best[key] for key in recipe.grams if key in best and best[key].offer is None}
        for key in recipe.grams:
            if to_buy[key] == 0:
                base[key] = IngredientPrice(
                    ingredient_id=key,
                    label=catalog.ingredients[key].label,
                    price_per_kg_czk=Decimal(0),
                )
        basket_options: list[dict[str, IngredientPrice]] = []
        for count in range(0, min(policy.max_stores, len(relevant_stores)) + 1):
            for subset in combinations(relevant_stores, count):
                basket = dict(base)
                for key in recipe.grams:
                    if to_buy[key] == 0:
                        continue
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
                sum(to_buy[key] * option[key].price_per_kg_czk for key in recipe.grams),
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
        cost = (
            Decimal(0)
            if catalog.pantry.seasonings_available
            else policy.seasoning_allowance_czk * policy.servings
        )
        lines: list[MealLine] = []
        stores: set[str] = set()
        for key, grams in recipe.grams.items():
            ingredient, price = catalog.ingredients[key], basket[key]
            edible = grams * ingredient.edible_fraction
            nutrients = nutrients.plus(ingredient.nutrients_per_100g_edible.scaled(edible / 100))
            usage = to_buy[key] / 1000 * price.price_per_kg_czk
            cost += usage
            if price.offer:
                stores.add(price.offer.product.store_id)
            lines.append(
                MealLine(
                    price=price,
                    required_grams=required[key],
                    owned_grams=owned[key],
                    purchased_grams=to_buy[key],
                    edible_grams=edible * policy.servings,
                    usage_cost_czk=usage.quantize(Decimal("0.01"), ROUND_HALF_UP),
                )
            )
        cost /= policy.servings
        if (
            nutrients.protein_g < policy.min_protein_g
            or nutrients.kcal > policy.max_kcal
            or cost > policy.max_cost_per_serving_czk
        ):
            continue
        meals.append(
            PlannedMeal(
                title=recipe.title,
                meal_style=recipe.meal_style,
                minutes=recipe.minutes,
                servings=policy.servings,
                lines=tuple(lines),
                nutrients_per_serving=nutrients,
                usage_cost_per_serving_czk=cost.quantize(Decimal("0.01"), ROUND_HALF_UP),
                protein_g_per_czk=(
                    (nutrients.protein_g / cost).quantize(Decimal("0.001")) if cost else None
                ),
                use_first_grams=sum(
                    (
                        owned[key]
                        for key in recipe.grams
                        if key in catalog.pantry.items and catalog.pantry.items[key].use_first
                    ),
                    Decimal(0),
                ),
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
            -meal.use_first_grams,
            -(policy.ranking == "protein_per_czk" and meal.protein_g_per_czk is None),
            -(
                meal.protein_g_per_czk or Decimal(0)
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
        pantry=catalog.pantry,
        meals=tuple(meals),
        cheap_ingredients=tuple(value for value in best.values() if value.offer is not None),
        rejected_offers=dict(rejected),
        warnings=batch.warnings,
        source_latest_run_status=batch.latest_run_status,
    )
