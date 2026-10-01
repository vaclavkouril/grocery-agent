# Tesco Online Nákupy catalog

The `tesco` adapter reads the public Czech online shop at
[nakup.itesco.cz](https://nakup.itesco.cz/shop/cs-CZ/landing/groceries). It collects ordinary product
prices and advertised online promotions from department listings. It does not use a leaflet,
Kupi data, or a promotions-only list.

Tesco offers delivery and [Klikni+Vyzvedni pickup](https://nakup.itesco.cz/shop/cs-CZ/zone/klikni-a-vyzvedni).
This adapter collects catalog offers; it does not book a slot, add a basket or place an order.

## Run and requirements

Install the existing optional browser extra:

```sh
.venv/bin/python -m pip install -e '.[browser]'
```

Use installed Chromium, or install Playwright's browser if Chromium is not installed:

```sh
.venv/bin/python -m playwright install chromium
.venv/bin/grocery-agent scrape tesco
```

Run from the repository root. Tesco currently rejects direct HTTP and headless-browser catalog
requests on this machine, so the adapter defaults to an ordinary **visible Chromium browser**.
A graphical Linux session is required for that mode. It creates an isolated anonymous browser
session and closes it when the run finishes or fails; it never loads your personal browser profile.

Default coverage discovers all thirteen ordinary departments currently shown in navigation:
produce; dairy/eggs; bakery; meat/deli; frozen; cupboard staples; drinks; special diets; cleaning;
health/beauty; baby; pets; and home/entertainment. Curated “Top výběr” and “Novinky” lists are
excluded. Every selected department follows Tesco's own next-page links until its declared total
is covered. There is no default promotional filter. Catalog departments can change over time.

For a focused dataset:

```sh
GROCERY_TESCO_CATEGORY_SLUGS='["maso-a-lahudky","ovoce-a-zelenina","trvanlive"]' \
  .venv/bin/grocery-agent scrape tesco
```

The category override is a nonempty JSON list; omit it to discover all ordinary departments.

| Setting | Default / meaning |
| --- | --- |
| `GROCERY_TESCO_CATEGORY_SLUGS` | Unset: discover every ordinary public department |
| `GROCERY_TESCO_MAX_PAGES` | `500` per department; exceeding the cap fails coverage |
| `GROCERY_TESCO_REQUEST_DELAY_SECONDS` | `1`; also respects public robots crawl delay |
| `GROCERY_TESCO_ATTEMPTS` | `3`; retries only transient HTTP 429/502/503/504 |
| `GROCERY_TESCO_BROWSER_HEADLESS` | `false`; only enable if your normal headless browser is allowed by Tesco |
| `GROCERY_TESCO_BROWSER_EXECUTABLE` | Optional Chromium path; otherwise system Chromium or Playwright's browser |
| `GROCERY_TESCO_NAVIGATION_TIMEOUT_MS` | `30000` |

## Price mapping and comparison limits

- Retailer ID is `tesco`. The product-page numeric ID is the SKU. Kupi-derived Tesco SKUs keep
  their separate `kupi:` namespace.
- Fixed packs retain explicit contents from the product title, including multipacks. Unknown
  contents stay unknown. A source quote per piece can establish a one-piece item when its price
  agrees with the selling price. Product identity is not inferred from title or SKU.
- Loose/catch-weight goods use the quoted price per kg/g, with unknown exact pack contents.
  The representative item price is not treated as a fixed checkout price.
- Standard offers and Clubcard offers have separate stable keys. Clubcard prices retain the
  membership requirement. Ordinary price cuts preserve the displayed previous price and validity.
  Unsupported purchase conditions are retained as record errors rather than guessed.
- Explicit source availability is preserved. Missing availability stays unknown. No GTIN is
  invented; this adapter does not automatically match products with Rohlík or other stores.
- This adapter does not supply purchase terms for deposits or delivery/collection fees. These
  remain unknown; product prices do not establish a guaranteed whole-order total.

All offers use `scope = "tesco:online:anonymous"`. No delivery address, pickup store or time slot
has been selected, so this is the displayed anonymous catalog, not a national availability claim
or stock guaranteed for a particular pickup store. Tesco's checkout requires a user account and
fulfillment selection. Authenticated/store-specific acquisition is not implemented. A future
pickup comparison must use the chosen store's assortment, price and slot rather than relabel
these anonymous offers as pickup-specific.

## Source evidence and failure behavior

The adapter reads the live robots policy before navigating listings. It follows only unfiltered
Czech category URLs and validates page numbers, ranges, totals, product IDs and next-page links.
It deduplicates identical offers across departments and reports conflicting repeated facts.
An access denial, changed total, repeated/skipped products, incomplete pagination or missing
catalog state fails the run. Partial or failed batches cannot become complete comparison data.
HTTP 403 is not retried. No CAPTCHA solving, proxy rotation or browser-identity spoofing is used.

Snapshots retain public product markup, department links and coverage counts. Session/tracking
scripts, cart/account state, SVG icons and image URLs are removed. Each offer keeps its product
CSS locator, source URL and fetch timestamp. Parsing and normalization are pure and independent
of Playwright. Offline tests use captured public markup through the production parser and a
fixture browser session, including two-page coverage and persistence.

## Verification and current access limitation

On 2026-10-01, a normal visible browser read the online produce page: **24 actual products**,
including ordinary prices and four Clubcard variants (**28 canonical offers**), out of a displayed
**393-product** department. The captured source markup passes canonical validation. The browser
also read Tesco's live robots policy.

The production adapter persisted those **28 offers with zero record rejections** in an isolated
temporary test database, then correctly marked the run failed on page two. A local preview is
saved at `data/tesco/catalog-preview.json`, explicitly marked `complete = false`; it is not a
complete comparison batch. The full offline suite passed **321 tests**. Tesco's own source and
test files pass Ruff checks, and its four source modules pass mypy. At that verification stage,
repository-wide type/lint checks reported errors in application/database files being changed
in parallel. See [current verification](context.md#verification) for the combined tree.

Tesco rejected the same browser's published page-two link with **HTTP 403 Access Denied**.
Other attempts intermittently returned a page without catalog state, and headless/direct HTTP
access was denied. Consequently a complete live catalog scrape is **not verified or available
from this machine in this session**. The adapter stops visibly at those source failures; a
successful first page must not be described as the whole assortment. See the CLI run summary
and `grocery-agent runs` when reviewing coverage. Retry after source access recovers, using a
normal permitted browser session. The offline fixtures do not establish live completeness.

See [fixture notes](../tests/fixtures/tesco/README.md) for captured versus synthetic test data.
The default meal workflow still uses Kupi. Using Tesco for meals would require a fresh complete
Tesco batch and a meal policy with this source, scope and retailer; it does not merge sources.
