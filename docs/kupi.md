# Kupi acquisition

See [usage and requirements](usage.md) for installation and one-off acquisition-to-meal commands.

## Run

```sh
grocery-agent scrape kupi
grocery-agent runs --limit 2
```

The default covers 13 public grocery categories from the [Kupi index](https://www.kupi.cz/slevy):
fruit/vegetables; meat/poultry/fish/sausages; dairy/eggs; bakery; canned food; deli; frozen/instant
food; nonalcoholic drinks; alcohol; sweets/snacks; cooking/baking; health food; baby food/care.
The baby category also includes diapers and wipes. Kupi's electronics, furniture, clothing and
other general merchandise categories are outside the default grocery selection.
The HTTP adapter parses server-rendered HTML with BeautifulSoup/lxml; browser automation is unnecessary.
It follows every forward `page` link within each selected category, checks the source's robots rules,
spaces requests, and retries transient transport/429/502/503/504 failures with bounded attempts.
Redirects are rejected. It never requests Kupi's private/disallowed AJAX or outbound tracking routes.

`GROCERY_KUPI_CATEGORY_SLUGS`, `GROCERY_KUPI_LISTING_URL`, `GROCERY_KUPI_MAX_PAGES`, `GROCERY_KUPI_REQUEST_DELAY_SECONDS`, and
`GROCERY_KUPI_ATTEMPTS` configure the adapter through environment variables or `.env`.
Category slugs are a JSON list, e.g. `["maso-drubez-a-ryby","ovoce-a-zelenina"]`. Setting
`GROCERY_KUPI_LISTING_URL` overrides that list with a single category; remove an old fruit-only
override to use the expanded default. Only unfiltered `https://www.kupi.cz/slevy/<category>` URLs
are accepted. Page limits apply per category. Reaching a limit with a next page remaining fails
the run. A broken category is recorded and remaining categories are attempted; incomplete coverage
still fails the overall run. Access/rate denials (403/429) stop acquisition for the whole source.

Structured `category_started`, `category_finished` and `category_failed` events include the run ID,
category, page count, emitted items, duplicate count and parser errors. `emitted` is an acquisition
count; accepted/rejected/changed counts belong to the validating pipeline. Source snapshot metadata
also retains the category for durable per-category inspection.

## Mapping and limitations

| Source fact | Canonical field / behavior |
| --- | --- |
| Acquisition site | Adapter/run `source_id = kupi`; never used as the advertised retailer |
| Retailer link | `product.store_id`, e.g. `tesco`, `lidl`, `billa` |
| Kupi product group + quoted size | Namespaced `product.sku`, e.g. `kupi:7077:0.125kg` |
| Kupi campaign ID | `offer_key = kupi:<id>`; stable within that source campaign |
| Displayed locality | `scope = kupi:locality:<slug>`; first observed locality was Praha |
| Visible row price | Decimal `current_price`, currency CZK; `price_qualifier` distinguishes exact/from |
| Quoted quantity | Explicit price basis; known packs use one package plus physical contents |
| Visible discount percentage | `promotion.advertised_discount_percent`, reference `unspecified` |
| Loyalty label | Loyalty promotion and `requires_loyalty = true` |
| Retailer format, note and loyalty terms | Human-readable promotion conditions |
| Validity text | Inclusive local dates interpreted in Europe/Prague |
| Stock status | `unknown`; being advertised does not establish stock availability |

Source group IDs are comparison categories and may include several physical variants. They are not
retailer SKUs, GTINs, or resolved cross-store product IDs. No exact product matches are asserted.
Known pack contents require an explicit multipack or a note naming its contents (e.g. `vanička 1 kg`).
The word `baleno` alone does not establish pack weight. A per-kg/per-100-g meat quote retains its mass
price basis and unknown pack contents; explicit counter/weight-sale notes set `variable_weight`.
Multipacks such as `6x 0.5 l` become one package containing 3 l. Coffee doses use the distinct
canonical `serving` unit rather than pretending each dose is one physical capsule.
When Kupi omits quantity, the advertised price is per one opaque package with unknown contents,
and the inference is flagged in metadata; this cannot support a kg/l comparison. `cena od` remains
a lower-bound price through `price_qualifier = from`, including the derived unit price.
Unit prices normalize to kg, l, piece, package or serving, but equal units alone
do not establish equal quality, variety, pack contents or purchase conditions.

Kupi does not expose a regular price in these rows. The parser neither derives one from the displayed
percentage nor takes an aggregate/group price as a retailer price. `regular_price` and calculated
`discount_percent` remain unknown. Source percentage claims stay distinct from regular-price savings.

Short dates omit years. The adapter uses the nearest year within 183 days of the fetch/interval
anchor, handles December/January rollover, and retains the original text and inference policy in
source metadata. `dnes končí` ends today; `zítra končí` ends tomorrow; weekday-only dates such as
`ve st 30. 9.` start and end on that day. `aktuální` leaves both validity dates unknown. Upcoming
promotions are retained; consumers must check dates before treating an offer as usable today.

A new campaign creates a new offer; price corrections within the same campaign create history on
that offer. History across campaigns is available through the source StoreProduct. Direct retailer
adapters can coexist through their own SKU namespaces and scope. Reconciliation remains a matching
concern. Identical repeated sponsored rows are skipped; conflicting repeats are rejected with raw
evidence. Repeated campaigns across categories are also skipped: the first selected category provides
their canonical classification. Pagination progress is checked within each category, so a page of
already-seen campaigns cannot hide new campaigns further down that category.
Run counts describe distinct emitted campaigns rather than every visual duplicate.

There is no explicit locality selection yet. Runs use and record the locality shown by the source,
and fail if it changes while paginating. These are aggregated advertised promotions, not a complete
live retailer inventory. Complex bundle/tax/deposit semantics and geographic coverage beyond the
displayed listing need a later review before an optimizer uses them.

## Evidence and verification

Each emitted offer/rejection references full downloaded HTML bytes, URL, time, row locator and source
metadata. Shared pages are stored once by content hash; every accepted/rejected item retains its own
provenance record. Broken page markup or HTTP errors retain the response before failing the run.

Offline tests use reduced real fixtures and `httpx.MockTransport`; the suite forbids network access.
They exercise every default category's production parser, multiple retailer identities,
package/unit prices, loyalty, one-day dates, rollover, malformed rows, category failures, pagination
safety, cross-category deduplication, retries, robots rules, and SQLite history.

The original fruit-only live verification on 2026-09-30 found nine pages and 223 offers from 15 retailers,
all accepted with zero errors. An immediate repeated scrape accepted 223 and changed zero states.
This is an observed run, not an expected fixed daily count or a guarantee of retailer stock.

The expanded default was verified later that day: **7,848 accepted offers across 27 retailer IDs
and 281 listing pages**, with zero rejected items/errors. All 13 categories reached their final page.
The run took approximately 18 minutes 41 seconds, using the configured one-second request spacing
and synchronous local persistence. Counts below attribute overlapping campaigns to their first
selected category; the same campaign is counted once across the run.

| Category | Distinct offers contributed | Pages |
| --- | ---: | ---: |
| Fruit and vegetables | 226 | 9 |
| Meat, poultry, fish and sausages | 654 | 31 |
| Dairy and eggs | 977 | 32 |
| Bakery | 208 | 9 |
| Canned food | 284 | 11 |
| Deli | 296 | 13 |
| Frozen and instant food | 339 | 15 |
| Nonalcoholic drinks | 912 | 26 |
| Alcohol | 1,634 | 52 |
| Sweets and snacks | 977 | 30 |
| Cooking and baking | 702 | 28 |
| Health food | 289 | 13 |
| Baby food and care | 350 | 12 |
| **Total** | **7,848** | **281** |

These remain advertised promotions for the displayed locality, not every product stocked by those
retailers. Of the accepted quotes, 89 were explicitly starting prices and retain the `from` qualifier.
Parser changes to package interpretation and new canonical fields legitimately produce one new
state when re-ingesting older fruit-only observations. Offline repeated multi-category ingestion
continues to be checked for unchanged-state deduplication.
