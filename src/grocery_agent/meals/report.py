import html
import os
import tempfile
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

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


def render_html(report: MealReport, catalog: MealCatalog) -> str:
    esc = html.escape
    cache_warnings = "".join(
        f'<p class="warning">{esc(message)}</p>' for message in report.warnings
    )
    policy = report.policy
    local_date = report.generated_at.astimezone(ZoneInfo(policy.timezone)).date().isoformat()
    expiry = report.batch_finished_at + timedelta(hours=policy.max_age_hours)
    cards: list[str] = []
    for meal in report.meals:
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
                else esc(price.label)
            )
            store = (
                esc(offer.product.store_id)
                if offer
                else ("Already owned" if line.purchased_grams == 0 else "Pantry estimate")
            )
            rows.append(
                f"<tr><td>{product}</td><td>{line.required_grams:g} g</td>"
                f"<td>{line.owned_grams:g} g</td><td>{line.purchased_grams:g} g</td>"
                f"<td>{store}</td>"
                f"<td>{price.price_per_kg_czk:.2f} Kč/kg</td>"
                f"<td>{line.usage_cost_czk:.2f} Kč</td></tr>"
            )
            ingredient = catalog.ingredients[price.ingredient_id]
            sources.append(
                f'<li>{esc(ingredient.label)}: <a href="{esc(str(ingredient.nutrition_source))}">'
                f"nutrition source</a>; edible yield {ingredient.edible_fraction * 100:g}%</li>"
            )
            if offer:
                provenance = (
                    f"source: {esc(price.source_id or 'unspecified')}; "
                    f"run: {esc(price.run_id or 'unspecified')}; "
                    f"shopping context: {esc(price.shopping_context)}; "
                )
                promotion = offer.promotion
                dates = f"{offer.valid_from or 'start not specified'} → "
                dates += str(offer.valid_until or "end not specified")
                terms.append(
                    f"<li>{esc(offer.product.name)} — {esc(dates)}; "
                    f"{provenance}"
                    f"{esc(promotion.conditions or 'no terms reported') if promotion else ''}; "
                    f"stock: {esc(offer.availability.value)}; observed {price.observed_at}</li>"
                )
        steps = "".join(f"<li>{esc(step)}</li>" for step in meal.steps)
        seasoning_note = (
            "Seasonings already available: 0.00 Kč."
            if report.pantry.seasonings_available
            else (
                f"Plus {policy.seasoning_allowance_czk:.2f} Kč pantry seasoning "
                "allowance per serving."
            )
        )
        priority_note = (
            f"<p>Uses {meal.use_first_grams:g} g of ingredients marked use first.</p>"
            if meal.use_first_grams
            else ""
        )
        cards.append(
            f"<article><h2>{esc(meal.title)}</h2><p>{meal.minutes} minutes · "
            f"{meal.servings} serving(s) · "
            f"{esc(', '.join(meal.stores) or 'No store trip needed')}</p>"
            f'<div class="stats"><span class="stat"><b>{n.protein_g:.0f} g</b> protein</span>'
            f'<span class="stat">{n.kcal:.0f} kcal</span>'
            f'<span class="stat">{n.carbs_g:.0f} g carbs · {n.fat_g:.0f} g fat</span>'
            f'<span class="stat">≈ {meal.usage_cost_per_serving_czk:.2f} Kč / serving</span></div>'
            f"{priority_note}"
            '<div class="scroll"><table><thead><tr><th>Ingredient</th><th>Use</th>'
            "<th>Already have</th><th>Buy</th>"
            "<th>Retailer</th><th>Quote</th><th>Used cost</th></tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table></div>"
            f"<p class='note'>{seasoning_note} Table quantities cover all servings; "
            "macros and headline cost "
            "are per serving. Weigh rice/lentils dry and meat raw; vegetables before trimming.</p>"
            f"<h3>Cook it</h3><ol>{steps}</ol><details><summary>Offer terms and nutrition sources"
            f"</summary><ul>{''.join(terms)}</ul><ul>{''.join(sources)}</ul></details></article>"
        )
    priority_label = (
        "use-first stock, then "
        if any(item.use_first for item in report.pantry.items.values())
        else ""
    )
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Protein meals · {esc(policy.location_label)}</title><style>{STYLE}</style><main>"
        f"<p>GROCERY AGENT · {esc(policy.location_label)} · {local_date}</p>"
        "<h1>Good food. More protein.<br>Today's advertised prices.</h1>"
        f"<p>Meal style: {esc(policy.meal_style)} · "
        f"minimum {policy.min_protein_g:g} g protein / serving · "
        f"maximum {policy.max_kcal:g} kcal / serving</p>"
        f"<p>Lactose-free: {policy.lactose_free} · loyalty prices: {policy.allow_loyalty} · "
        f"ranked by {priority_label}"
        f"{esc(policy.ranking.replace('_', ' '))}</p>"
        f"{cache_warnings}"
        '<p class="warning" id="freshness">For the report date only. Check offer dates and outlet '
        "terms before shopping. Availability is not guaranteed.</p>"
        "<p class='note'>Costs estimate usage, not a checkout total. Whole packs may cost "
        "more; unknown pack sizes and travel are not priced. Already-owned quantities cost zero; "
        "oil and spices use estimates unless marked available. Each dish is an alternative using "
        "the same stock, not a combined shopping plan. Stock is not deducted automatically; "
        "update it after cooking. Macros use generic ingredient data, trimming yields, "
        "and include oil; optional "
        "seasonings are excluded. Select plain ingredients and check labels for lactose.</p>"
        + "".join(cards)
        + f"<footer>Generated {report.generated_at.isoformat()} · batch "
        f"{report.batch_finished_at.isoformat()} · run {esc(report.run_id)}</footer></main>"
        f"<script>const day=new Intl.DateTimeFormat('en-CA',{{timeZone:'{esc(policy.timezone)}',"
        "year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());"
        f"if(day!=='{local_date}'||Date.now()>Date.parse('{expiry.isoformat()}'))"
        "document.getElementById('freshness').textContent="
        "'This report is out of date. Run grocery-agent meals to generate current suggestions.';"
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
