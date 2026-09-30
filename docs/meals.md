# One-off protein meal workflow

Run from the project root:

```sh
# Use the latest complete, fresh collection (no network calls):
.venv/bin/grocery-agent meals

# Or acquire new offers first; one execution, no timer or background service:
.venv/bin/grocery-agent workflow
```

Open `data/reports/latest.html` in a browser. `latest.json` is machine-readable and timestamped
JSON reports retain the recommendation inputs. Reports contain recipe steps, macros, shopping
quantities, retailer links, prices, validity, stock uncertainty, and the originating run ID.
The full default acquisition covers 13 categories and currently takes about 19 minutes.
No scheduler is installed or activated by these commands.

## Current profile

`config/meals.toml` configures Praha, lactose-free ingredients, one large serving, at least 70 g
protein, at most 850 kcal and 100 Kč in ingredients used per serving, and at most two supermarket
chains. These are practical defaults for recipe ranking, not personalized nutrition targets.
The configured ordinary supermarket allowlist can be changed or emptied to consider all sellers.
Ranking maximizes protein per koruna among the configured fixed recipes. Set `ranking = "protein"`
to prioritize absolute protein. Three initial recipe templates are provided; this is not a global
optimum across every possible food or dish. No LLM or framework is involved.

## Boundaries and correctness

- `CurrentOfferReader` supplies a streamed, complete acquisition batch to the planner. Its SQL
  implementation reads run membership, not every historical offer. Disappeared offers are excluded.
  Latest failed, partial, empty, cancelled, or unfinished runs block report generation; there is no
  silent fallback to an earlier successful run. Both batch and item timestamps must be within 36 h.
- Ingredient matching uses conservative name patterns, independently of acquisition sources and
  retailer identity. It does not establish cross-store product identity or rewrite the product
  catalog. Processed/marinated/bone-in food is excluded from plain raw ingredient nutrition.
- Prices must be exact, positive CZK quotes with a known mass unit. Expired/upcoming offers are
  excluded using inclusive dates in Europe/Prague. Loyalty prices default to excluded. Coupons,
  multi-buy offers, ambiguous quantity ranges and visible free-item bundle terms are excluded.
  Unspecified start/end dates are displayed, never invented. Unknown stock is reported as unknown.
- Generic basket search chooses the least usage cost within the retailer-count limit; it uses
  retailer IDs only as opaque identities. No consumer branches on a particular retailer.
- Money and nutrient arithmetic use Decimal. Ingredients are weighed raw/dry before cooking.
  Macros use the edible portion; source vegetable trimming coefficients are explicitly configured.
  Actual varieties, labels, yields and cooking losses can differ, so nutrition is an estimate.
  The carbohydrate field uses available carbohydrates from the Czech database.
- Recipe ingredients are naturally dairy-free plain foods. Check actual labels, especially
  seasoning blends, rather than assuming a product title certifies absence of lactose.

## Cost meaning

The headline cost is for ingredients **used**, not a guaranteed checkout total. A 300 g recipe
portion quoted per kg may require buying a larger pack. The source often does not expose actual
pack contents, so the planner does not invent purchase sizes or a checkout total. Travel costs and
store-level stock are unknown. Even a two-chain basket may need different outlets; check the source
terms. Oil uses an explicit pantry estimate of 200 Kč/kg, and seasonings an allowance of 3 Kč per
serving; these are editable estimates rather than scraped prices. Oil is included in macros;
optional seasoning/vinegar amounts are not.

## Nutrition sources

The small curated catalog records energy, protein, available carbohydrates and fat per 100 g
edible portion from the Czech Food Composition Database, checked 2026-10-01:

- [Raw skinless chicken breast](https://www.nutridatabaze.cz/potraviny/?id=230)
- [Raw boneless skinless turkey breast](https://www.nutridatabaze.cz/potraviny/?id=452)
- [Dry red lentils](https://www.nutridatabaze.cz/potraviny/?id=647)
- [Dry white rice](https://www.nutridatabaze.cz/potraviny/?id=181)
- [Raw carrot](https://www.nutridatabaze.cz/potraviny/?id=62)
- [Raw onion](https://www.nutridatabaze.cz/potraviny/?id=51)
- [Rapeseed oil](https://www.nutridatabaze.cz/potraviny/?id=84)

The catalog preserves source URLs. Values are generic food estimates, not scraped product labels.
Adding recipes or ingredients requires configuration and offline tests; acquisition adapters and
business persistence remain unchanged.

## Operations

`GROCERY_MEAL_CONFIG`, `GROCERY_REPORT_DIR`, and `GROCERY_LOCK_PATH` select configuration, output,
and a shared advisory lock. All CLI acquisitions and report writes use this lock; concurrent jobs
fail visibly instead of overlapping. Embedded users of the pipeline must coordinate the same lock.
The OS releases it on exit/crash. Runtime data is ignored by Git. Individual report files are
atomically replaced. JSON is authoritative; multi-file replacement is not a transactional bundle.

The fresh-acquisition workflow replaces the latest view with a pending notice before acquisition
and a failure notice if acquisition/planning fails; the CLI exits nonzero. Date/freshness checks in
the HTML also warn when a previously opened report ages out. No agent, alert delivery, stale-run
repair, serving web app, or scheduling is implemented.
