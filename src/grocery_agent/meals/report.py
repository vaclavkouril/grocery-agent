import html
import json
import os
import tempfile
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from grocery_agent.localization import (
    Language,
    format_date,
    format_decimal,
    localized_notice,
    translate,
)
from grocery_agent.meals.catalog import MealCatalog
from grocery_agent.meals.planner import MealReport

STYLE = """
body{font:16px/1.6 system-ui,sans-serif;color:#162e26;background:#f3f6f0;margin:0}
main{max-width:1000px;margin:auto;padding:36px 20px}h1{font-size:2.5rem;line-height:1.15}
article{background:white;padding:24px;margin:24px 0;border-radius:18px;border:1px solid #dce5d8}
.stats{display:flex;flex-wrap:wrap;gap:12px}
.stat{background:#e5f1e3;padding:10px 16px;border-radius:10px}
table{width:100%;border-collapse:collapse}
td,th{text-align:left;padding:10px;border-bottom:1px solid #ddd}
a{color:#176449}small,.note{color:#526258}.warning{background:#ffe3c0;padding:14px;border-radius:10px}
.scroll{overflow-x:auto}li{margin:10px 0}details{margin:14px 0}
@media print{
@page{margin:15mm}
body{background:white;color:black;font-size:11pt}
main{max-width:none;padding:0}h1{font-size:24pt}
article{padding:12px;margin:16px 0;border-radius:0}
.stat,.warning{background:transparent;border:1px solid #999}
.scroll{overflow:visible}table{font-size:9pt}td,th{padding:5px;overflow-wrap:anywhere}
h2,h3,summary{break-after:avoid}tr,li,.stats{break-inside:avoid}
thead{display:table-header-group}a{color:black}
details::details-content{content-visibility:visible}
details>:not(summary){display:block!important}
}
"""


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as f:
        temporary = Path(f.name)
        try:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def render_html(report: MealReport, catalog: MealCatalog, language: Language = "en") -> str:
    esc = html.escape

    def t(message: str, **values: object) -> str:
        return esc(translate(message, language, **values))

    def d(value: Decimal, spec: str = "g") -> str:
        return format_decimal(value, language, spec)

    def timestamp(value: datetime) -> str:
        return format_date(
            value.astimezone(ZoneInfo(catalog.policy.timezone)) if language == "cs" else value,
            language,
        )

    cache_warnings = "".join(
        f'<p class="warning">{esc(localized_notice(message, language))}</p>'
        for message in report.warnings
    )
    policy = report.policy
    local_date = report.generated_at.astimezone(ZoneInfo(policy.timezone)).date().isoformat()
    display_date = format_date(
        report.generated_at.astimezone(ZoneInfo(policy.timezone)).date(), language
    )
    expiry = report.batch_finished_at + timedelta(hours=policy.max_age_hours)
    cards: list[str] = []
    for meal in report.meals:
        template = next(
            (
                recipe
                for recipe in catalog.recipes
                if recipe.title == meal.title and recipe.steps == meal.steps
            ),
            None,
        )
        title = template.titles.get(language, meal.title) if template else meal.title
        translated_steps = (
            template.translated_steps.get(language, meal.steps) if template else meal.steps
        )
        n = meal.nutrients_per_serving
        rows: list[str] = []
        sources: list[str] = []
        terms: list[str] = []
        for line in meal.lines:
            price = line.price
            offer = price.offer
            product = (
                f'<a href="{esc(str(offer.source_url))}">{esc(offer.product.name)}</a>'
                if offer
                else esc(catalog.ingredients[price.ingredient_id].localized_label(language))
            )
            store = (
                esc(offer.product.store_id)
                if offer
                else t("Already owned" if line.purchased_grams == 0 else "Pantry estimate")
            )
            rows.append(
                f"<tr><td>{product}</td><td>{d(line.required_grams)} g</td>"
                f"<td>{d(line.owned_grams)} g</td><td>{d(line.purchased_grams)} g</td>"
                f"<td>{store}</td>"
                f"<td>{d(price.price_per_kg_czk, '.2f')} Kč/kg</td>"
                f"<td>{d(line.usage_cost_czk, '.2f')} Kč</td></tr>"
            )
            ingredient = catalog.ingredients[price.ingredient_id]
            sources.append(
                f"<li>{esc(ingredient.localized_label(language))}: "
                f'<a href="{esc(str(ingredient.nutrition_source))}">'
                f"{t('nutrition source')}</a>; {t('edible yield')} "
                f"{d(ingredient.edible_fraction * 100)}%</li>"
            )
            if offer:
                source = esc(price.source_id) if price.source_id else t("unspecified")
                provenance = (
                    f"{t('source')}: {source}; "
                    f"{t('run')}: {esc(price.run_id) if price.run_id else t('unspecified')}; "
                    f"{t('shopping context')}: {esc(price.shopping_context)}; "
                )
                promotion = offer.promotion
                dates = (
                    format_date(offer.valid_from, language)
                    if offer.valid_from
                    else translate("start not specified", language)
                ) + " → "
                dates += (
                    format_date(offer.valid_until, language)
                    if offer.valid_until
                    else translate("end not specified", language)
                )
                conditions = (
                    (esc(promotion.conditions) if promotion.conditions else t("no terms reported"))
                    if promotion
                    else ""
                )
                observed = timestamp(price.observed_at) if price.observed_at else t("unspecified")
                terms.append(
                    f"<li>{esc(offer.product.name)} — {esc(dates)}; "
                    f"{provenance}"
                    f"{conditions}; "
                    f"{t('stock')}: {t(offer.availability.value)}; "
                    f"{t('observed')} {observed}</li>"
                )
        steps = "".join(f"<li>{esc(step)}</li>" for step in translated_steps)
        seasoning_note = (
            t("Seasonings already available: 0.00 Kč.")
            if report.pantry.seasonings_available
            else (
                t(
                    "Plus {cost} Kč pantry seasoning allowance per serving.",
                    cost=d(policy.seasoning_allowance_czk, ".2f"),
                )
            )
        )
        priority_note = ""
        if meal.use_first_grams:
            priority_note = (
                "<p>"
                + t(
                    "Uses {grams} g of ingredients marked use first.",
                    grams=d(meal.use_first_grams),
                )
                + "</p>"
            )
        cards.append(
            f"<article><h2>{esc(title)}</h2><p>{meal.minutes} {t('minutes')} · "
            f"{meal.servings} {t('serving(s)')} · "
            f"{esc(', '.join(meal.stores)) if meal.stores else t('No store trip needed')}</p>"
            '<div class="stats"><span class="stat">'
            f"<b>{d(n.protein_g, '.0f')} g</b> {t('protein')}</span>"
            f'<span class="stat">{d(n.kcal, ".0f")} kcal</span>'
            f'<span class="stat">{d(n.carbs_g, ".0f")} g {t("carbs")} · '
            f"{d(n.fat_g, '.0f')} g {t('fat')}</span>"
            f'<span class="stat">≈ {d(meal.usage_cost_per_serving_czk, ".2f")} Kč '
            f"/ {t('serving')}</span></div>"
            f"{priority_note}"
            '<div class="scroll"><table><thead><tr>'
            + "".join(
                f"<th>{t(label)}</th>"
                for label in (
                    "Ingredient",
                    "Use",
                    "Already have",
                    "Buy",
                    "Retailer",
                    "Quote",
                    "Used cost",
                )
            )
            + "</tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table></div>"
            f"<p class='note'>{seasoning_note} "
            + t(
                "Table quantities cover all servings; macros and headline cost are per serving. "
                "Weigh rice/lentils dry and meat raw; vegetables before trimming."
            )
            + "</p>"
            f"<h3>{t('Cook it')}</h3><ol>{steps}</ol><details><summary>"
            f"{t('Offer terms and nutrition sources')}"
            f"</summary><ul>{''.join(terms)}</ul><ul>{''.join(sources)}</ul></details></article>"
        )
    priority_label = (
        t("use-first stock, then ")
        if any(item.use_first for item in report.pantry.items.values())
        else ""
    )
    advertised_prices = t("Today's advertised prices.")
    return (
        f'<!doctype html><html lang="{language}"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{t('Protein meals')} · {esc(policy.location_label)}</title>"
        f"<style>{STYLE}</style><main>"
        f"<p>GROCERY AGENT · {esc(policy.location_label)} · "
        f"{display_date}</p>"
        f"<h1>{t('Good food. More protein.')}<br>{advertised_prices}</h1>"
        f"<p>{t('Meal style')}: {t(policy.meal_style)} · "
        f"{t('minimum')} {d(policy.min_protein_g)} g {t('protein')} / {t('serving')} · "
        f"{t('maximum')} {d(policy.max_kcal)} kcal / {t('serving')}</p>"
        f"<p>{t('Lactose-free')}: {t(str(policy.lactose_free))} · "
        f"{t('loyalty prices')}: {t(str(policy.allow_loyalty))} · "
        f"{t('ranked by')} {priority_label}"
        f"{t(policy.ranking.replace('_', ' '))}</p>"
        f"{cache_warnings}"
        '<p class="warning" id="freshness">'
        + t(
            "For the report date only. Check offer dates and outlet terms before shopping. "
            "Availability is not guaranteed."
        )
        + "</p>"
        "<p class='note'>"
        + t(
            "Costs estimate usage, not a checkout total. Whole packs may cost "
            "more; unknown pack sizes and travel are not priced. "
            "Already-owned quantities cost zero; oil and spices use estimates unless marked "
            "available. Each dish is an alternative using "
            "the same stock, not a combined shopping plan. Stock is not deducted automatically; "
            "update it after cooking. Macros use generic ingredient data, trimming yields, "
            "and include oil; optional "
            "seasonings are excluded. Select plain ingredients and check labels for lactose."
        )
        + "</p>"
        + "".join(cards)
        + f"<footer>{t('Generated')} {timestamp(report.generated_at)} · {t('batch')} "
        f"{timestamp(report.batch_finished_at)} · {t('run')} {esc(report.run_id)}</footer></main>"
        f"<script>const day=new Intl.DateTimeFormat('en-CA',{{timeZone:'{esc(policy.timezone)}',"
        "year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());"
        f"if(day!=='{local_date}'||Date.now()>Date.parse('{expiry.isoformat()}'))"
        "document.getElementById('freshness').textContent="
        + json.dumps(
            translate(
                "This report is out of date. Run grocery-agent meals "
                "to generate current suggestions.",
                language,
            )
        )
        + ";"
        "</script></html>"
    )


def save_report(directory: Path, report: MealReport, catalog: MealCatalog) -> None:
    content = report.model_dump_json(indent=2, exclude_computed_fields=True)
    # Timestamped JSON keeps an audit trail. Latest JSON is authoritative; HTML is a view.
    stamp = report.generated_at.strftime("%Y%m%dT%H%M%S%fZ")
    atomic_write(directory / f"{stamp}.json", content)
    atomic_write(directory / "latest.json", content)
    atomic_write(directory / "latest.html", render_html(report, catalog))


def save_failure(directory: Path, message: str) -> None:
    import json

    atomic_write(directory / "latest.json", json.dumps({"status": "failed", "error": message}))
    atomic_write(
        directory / "latest.html",
        "<!doctype html><meta charset='utf-8'>"
        f"<h1>Workflow failed</h1><p>{html.escape(message)}</p>"
        "<p>No current meal recommendation. Inspect grocery-agent runs and the journal.</p>",
    )
