# One-off protein meal workflow

For prerequisites, installation, a focused acquisition command, configuration examples and
troubleshooting, see [usage and requirements](usage.md).

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
to prioritize absolute protein. Four main-dish templates remain the default; optional breakfasts
and snacks add four templates. This is not a global
optimum across every possible food or dish. No LLM or framework is involved.

## Meal styles

```sh
.venv/bin/grocery-agent meals --meal-style main
.venv/bin/grocery-agent meals --meal-style breakfast --have oats=1kg
.venv/bin/grocery-agent meals --meal-style snack --have chicken=2kg --have oil=available
.venv/bin/grocery-agent meals --meal-style breakfast --min-protein 40 --budget 50
.venv/bin/grocery-agent command 'meal meal_style=snack have=chicken=2kg min_protein_g=30'
```

The same options work with `workflow`. Main dishes are the primary/default purpose. Selecting
a different style applies its configured `[style_defaults.<style>]` limits, then explicit CLI/text
limits override them. Unchanged saved profiles retain their own limits. Returning from a saved
breakfast/snack profile to `main` restores the main catalog limits unless the request supplies others.

| Style | Templates | Minimum protein | Maximum kcal | Usage-cost limit |
| --- | --- | --- | --- | --- |
| `main` | Four existing chicken/lentil/rice and turkey dishes | 70 g | 850 | 100 Kč |
| `breakfast` | Savory chicken oat porridge; chicken rice porridge | 35 g | 650 | 80 Kč |
| `snack` | Paprika chicken bites/carrot sticks; mini turkey patties/carrots | 25 g | 450 | 60 Kč |

All values are per serving and configurable. These are savory, lactose-free, protein-focused
templates; no automatic portion adjustment, sweet dairy-based foods or whole-day meal allocation
is introduced. The planner filters to the selected style, enforces the same freshness/retailer/
loyalty rules and calculates full macros. `--have` and `--use-first` work for every style.
Dry plain oats can be bought from eligible mass quotes or supplied as `--have oats=1kg`.
Breakfast without an oat offer can still use the rice template. Adding more styles/recipes stays
inside meal configuration and validation, independently of retailer adapters and databases.

## Boundaries and correctness

- `CurrentOfferReader` supplies a streamed, complete acquisition batch to the planner. Its SQL
  implementation reads run membership, not every historical offer. Disappeared offers are excluded.
  The published reader can use the last complete cache after an unsuccessful refresh within its
  freshness limit, carrying explicit warnings. `--strict-latest` retains refusal after failed,
  partial, empty, cancelled or running attempts. Both batch and items must be fresh; expired and
  upcoming offers stay excluded. See [catalogue policy](catalogue.md#freshness-and-expiry).
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

Already-owned pantry quantities are deducted before pricing the missing mass across all servings.
They contribute full nutrition and zero new purchase cost. Oil and seasoning estimates remain
unless explicitly covered by pantry stock. `use_first` stock ranks recipes by owned priority
mass used before the configured ranking. Fully covered dishes can cost zero; their protein per
koruna is stored as JSON `null`, and they rank first under `protein_per_czk` when priority mass
ties. Each suggested dish independently uses the same pantry stock. Reports preserve the pantry
inputs and separate required, owned and purchased grams; `purchased_grams` now means the shortage
to buy, while `required_grams` is the full recipe amount before trimming. Stocks are not consumed
automatically. See [pantry options and configuration](usage.md#food-you-already-have).

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
- [Plain dry oat flakes](https://www.nutridatabaze.cz/potraviny/?id=188)

The catalog preserves source URLs. Values are generic food estimates, not scraped product labels.
Adding recipes or ingredients requires configuration and offline tests; acquisition adapters and
business persistence remain unchanged.

## Operations

`GROCERY_MEAL_CONFIG`, `GROCERY_REPORT_DIR`, and `GROCERY_LOCK_PATH` select configuration, output,
and a shared advisory lock. CLI acquisitions and local `latest` publication use this lock;
`--no-latest` meal reads write isolated request directories without blocking collection.
Embedded acquisition callers must coordinate the same writer lock.
The OS releases it on exit/crash. Runtime data is ignored by Git. Individual report files are
published in a per-request directory with parameters/catalog/run manifest. Local `latest` aliases
are separate atomically replaced convenience files, not a transactional private result bundle.

The fresh-acquisition workflow replaces the latest view with a pending notice before acquisition
and a failure notice if acquisition/planning fails; the CLI exits nonzero. Date/freshness checks in
the HTML also warn when a previously opened report ages out. A separate optional collector and
private catalogue read API are implemented. No agent, alert delivery, stale-run repair, accounts,
public meal controls, email or SimpleX transport is active. See [phase 1](phase-one.md).
