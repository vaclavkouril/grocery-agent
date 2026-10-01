# Rohlík online catalog

Rohlík is registered as `rohlik`. It uses the public anonymous catalog at
[www.rohlik.cz](https://www.rohlik.cz/); no retailer account or new dependencies are needed.
Offers use the existing validation, snapshots and price-history pipeline.

## Run

From the repository root:

```sh
.venv/bin/grocery-agent stores
.venv/bin/grocery-agent scrape rohlik
.venv/bin/grocery-agent runs --limit 5
```

Default coverage is eleven top-level food/drink categories, including meat, dairy, produce,
bakery, deli, frozen, plant-based foods, cupboard staples, drinks, special diets and ready meals.
This includes ordinary prices as well as advertised discounts. Some categories also contain
related nonfood goods. Pharmacy and household categories are excluded by default.

For a focused comparison dataset:

```sh
GROCERY_ROHLIK_CATEGORY_IDS='[300103000,300102000,300106000]' \
  .venv/bin/grocery-agent scrape rohlik
```

Or add the JSON category list to `.env`. IDs are checked against current public navigation.
For one category use `[300102000]` (produce), `[300103000]` (meat) or `[300105000]` (dairy).
The selection currently supports the top-level categories exposed in navigation.

| Setting | Default / meaning |
| --- | --- |
| `GROCERY_ROHLIK_CATEGORY_IDS` | JSON list of eleven food/drink category IDs |
| `GROCERY_ROHLIK_PAGE_SIZE` | `100`; 1–100 products per page |
| `GROCERY_ROHLIK_MAX_PAGES` | `100` per category; exceeding the cap fails coverage |
| `GROCERY_ROHLIK_REQUEST_DELAY_SECONDS` | `1`; public robots crawl delay also applies |
| `GROCERY_ROHLIK_ATTEMPTS` | `3`; bounded transient transport/429/502/503/504 retries |
| `GROCERY_ROHLIK_EXPECTED_WAREHOUSE_ID` | Optional positive ID; reject another public warehouse |

## Location and pricing

The anonymous homepage identifies its displayed locality and warehouse. Offers carry a scope
such as `rohlik:online:warehouse:8791:locality:praha-7`, observed on 2026-10-01. The adapter
checks the context again at completion. These prices describe that anonymous online catalog;
they are not a national quote or a promise of delivery availability at your address. The warehouse
setting checks coverage; it does not select a warehouse or enter a delivery address.

| Source fact | Canonical mapping |
| --- | --- |
| `productId` | Rohlík's own SKU, `store_id = rohlik` |
| `name`, `brand`, navigation category | Product descriptions, without inferring a brand from the name |
| Fixed `textualAmount` | Total physical contents, including explicit multipacks |
| `weightedItem = true`, `unit = kg` | `current_price = prices.unitPrice`, price basis 1 kg, unknown exact pack mass |
| Fixed-size item price | One package basis; known contents produce a normalized kg/l/piece price |
| Ordinary `salePrice` | Standard offer with price cut and known original price for fixed packs |
| `salePrice` without `originalPrice` | Advertised price, with unknown regular price and no calculated savings |
| Weighted sale | Advertised promotion; approximate original item price cannot establish regular Kč/kg |
| `pro vás`, Xtra/premium/club label | Conditional `loyalty` offer; fixed goods also emit a standard original-price offer |
| Explicit source stock status | Available/unavailable; unfamiliar statuses stay unknown |
| `saleValidTill` | Inclusive Czech date, conservatively rounded down for an expiry earlier than 23:59 |

Unknown quantities stay unknown; approximate text such as `cca 220 g` never becomes exact
contents. Units outside the domain, such as metres, leave contents unknown and retain the sale
package quote. All raw fields remain in evidence. Product cards expose no reliable GTIN in the
observed responses, so these products remain unmatched across stores. Rohlík SKUs are not
substituted for GTINs. Cross-store product matching and a comparison UI remain separate work.

“For you” sale labels do not fully describe eligibility. They are conservatively membership-
dependent with a condition to confirm eligibility at Rohlík; automatic unconditional comparison
must exclude them. Weighted conditional cards do not supply an exact regular mass quote, so
no standard mass price is invented. Unrecognized sale conditions produce retained record errors.
This adapter does not supply purchase terms for deposits, delivery or other checkout fees;
they remain unknown and are excluded from its merchandise prices.

The model retains calendar dates rather than expiry timestamps. The raw timestamp is saved in
source evidence. A morning expiry is rounded to the preceding date so downstream date filters
cannot use the discount after its actual deadline; this may exclude a still-active sale early.

## Acquisition and verification

The adapter reads `/robots.txt` and the homepage's public `__NEXT_DATA__` context, then uses
the same public JSON requests as the website:

- `/api/v1/categories/normal/<id>/products/count`
- `/api/v1/categories/normal/<id>/products?page=<zero-based>&size=<n>&sort=price-asc`
- `/api/v1/products/card?products=<comma-separated IDs>&categoryType=normal`

It streams each page, checks requested card coverage and category counts, rejects repeated or
backward pagination, and deduplicates identical offers across categories. Changed duplicate
facts are retained as rejections. Price ordering avoids the personalized recommendation order
observed to shift across requests. A changed count, location, incomplete page sequence, malformed
source or access denial fails the run. Record errors retain their exact JSON evidence and allow
other records to continue. A failed or partial run cannot become a complete comparison batch.

Public JSON is parsed directly with `Decimal`. Source-only fields are kept in retained raw bytes,
with a JSON record locator, fetch timestamp, warehouse, locality and category metadata.
Redirects fail; source authentication and shopping actions are not implemented.

These are website-internal public endpoints, without a supported API contract. Fixtures captured
on 2026-10-01 exercise the production parser and offline HTTP adapter contract. Offline tests
also cover exact unit pricing, conditional discounts, stock, malformed data, pagination, count and
location drift, robots restrictions, retries, persistence and unchanged price history. See
[fixture notes](../tests/fixtures/rohlik/README.md) for the reduced captures.

Live verification on 2026-10-01 completed the 715-product produce category with **759 accepted
offers**, including simultaneous standard/conditional variants, **zero rejections** and **zero
source errors**, using an isolated temporary database. Samples of 100 products from each of the
eleven default categories also passed parsing and canonical validation (1,100 products total).
The full offline suite passed 289 tests, and Ruff formatting/lint plus mypy passed. This is a
dated verification result; catalog sizes and prices change.

The meal workflow still defaults to Kupi. To use a fresh complete Rohlík batch for meals, update
`[policy]` in your meal TOML to `source_id = "rohlik"`, the exact observed `scope`, the matching
`location_label`, and `retailers = ["rohlik"]`. The planner consumes one configured source at a
time; it does not merge Rohlík and Kupi into a combined basket comparison.
