# Kupi acquisition

## Run

```sh
grocery-agent scrape kupi
grocery-agent runs --limit 2
```

The default is the public [fruit-and-vegetable category](https://www.kupi.cz/slevy/ovoce-a-zelenina).
The HTTP adapter parses server-rendered HTML with BeautifulSoup/lxml; browser automation is unnecessary.
It follows only forward `page` links within the configured category, checks the source's robots rules,
spaces requests, and retries transient transport/429/502/503/504 failures with bounded attempts.
Redirects are rejected. It never requests Kupi's private/disallowed AJAX or outbound tracking routes.

`GROCERY_KUPI_LISTING_URL`, `GROCERY_KUPI_MAX_PAGES`, `GROCERY_KUPI_REQUEST_DELAY_SECONDS`, and
`GROCERY_KUPI_ATTEMPTS` configure the adapter through environment variables or `.env`.
Only unfiltered `https://www.kupi.cz/slevy/<category>` URLs are accepted. Other categories have not
been verified yet; unsupported quantity/date formats are rejected visibly. Reaching the page limit
while a next page remains is a failure, not complete coverage.

## Mapping and limitations

| Source fact | Canonical field / behavior |
| --- | --- |
| Acquisition site | Adapter/run `source_id = kupi`; never used as the advertised retailer |
| Retailer link | `product.store_id`, e.g. `tesco`, `lidl`, `billa` |
| Kupi product group + quoted size | Namespaced `product.sku`, e.g. `kupi:7077:0.125kg` |
| Kupi campaign ID | `offer_key = kupi:<id>`; stable within that source campaign |
| Displayed locality | `scope = kupi:locality:<slug>`; first observed locality was Praha |
| Visible row price | Exact Decimal `current_price`, currency CZK |
| Quoted quantity | Explicit price basis; known packs use one package plus physical contents |
| Visible discount percentage | `promotion.advertised_discount_percent`, reference `unspecified` |
| Loyalty label | Loyalty promotion and `requires_loyalty = true` |
| Retailer format, note and loyalty terms | Human-readable promotion conditions |
| Validity text | Inclusive local dates interpreted in Europe/Prague |
| Stock status | `unknown`; being advertised does not establish stock availability |

Source group IDs are comparison categories and may include several physical variants. They are not
retailer SKUs, GTINs, or resolved cross-store product IDs. No exact product matches are asserted.
Known pack contents are populated only when the note explicitly indicates packaging; an unlabeled
per-kg offer retains a mass price basis and unknown package contents. `variable_weight` is set only
for an explicit weight-sale statement. Unit prices normalize to kg, l or piece, but equal units alone
do not establish equal quality, variety, pack contents or purchase conditions.

Kupi does not expose a regular price in these rows. The parser neither derives one from the displayed
percentage nor takes an aggregate/group price as a retailer price. `regular_price` and calculated
`discount_percent` remain unknown. Source percentage claims stay distinct from regular-price savings.

Short dates omit years. The adapter uses the nearest year within 183 days of the fetch/interval
anchor, handles December/January rollover, and retains the original text and inference policy in
source metadata. `dnes končí` ends today; `aktuální` leaves both validity dates unknown. Upcoming
promotions are retained; consumers must check dates before treating an offer as usable today.

A new campaign creates a new offer; price corrections within the same campaign create history on
that offer. History across campaigns is available through the source StoreProduct. Direct retailer
adapters can coexist through their own SKU namespaces and scope. Reconciliation remains a matching
concern. Identical repeated sponsored rows are skipped; conflicting repeats are rejected with raw
evidence. Run counts describe distinct emitted campaigns rather than every visual duplicate.

There is no explicit locality selection yet. Runs use and record the locality shown by the source,
and fail if it changes while paginating. These are aggregated advertised promotions, not a complete
live retailer inventory. Complex bundle/tax/deposit semantics and geographic coverage beyond the
displayed listing need a later review before an optimizer uses them.

## Evidence and verification

Each emitted offer/rejection references full downloaded HTML bytes, URL, time, row locator and source
metadata. Shared pages are stored once by content hash; every accepted/rejected item retains its own
provenance record. Broken page markup or HTTP errors retain the response before failing the run.

Offline tests use reduced real fixtures and `httpx.MockTransport`; the suite forbids network access.
They exercise the production parser, multiple retailer identities, package/unit prices, loyalty,
date rollover, malformed rows, pagination safety, retries, robots rules, and SQLite history.

Manual live verification on 2026-09-30 found nine pages and 223 distinct offers from 15 retailers,
all accepted with zero errors. An immediate repeated scrape accepted 223 and changed zero states.
This is an observed run, not an expected fixed daily count or a guarantee of retailer stock.
