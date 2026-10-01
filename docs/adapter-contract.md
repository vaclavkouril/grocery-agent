# Store adapter contract

```python
class AcquisitionAdapter(ABC):
    @property
    @abstractmethod
    def source_id(self) -> str: ...

    @abstractmethod
    def validate_offer(self, offer: Offer) -> None: ...

    @abstractmethod
    def fetch_offers(self, context: AdapterContext) -> AsyncIterator[AcquisitionItem]: ...
```

`StoreAdapter` extends this base for a single retailer: implement `store_id`, and it supplies
`source_id = store_id` plus the check that each candidate belongs to that retailer.
Aggregators implement the base directly: `source_id` identifies acquisition, while each candidate's
`product.store_id` identifies its advertised retailer. `validate_offer` raises `ValueError` when
source identity, key namespace, or scope is inconsistent. The pipeline always runs Pydantic
validation first, then this source boundary check. It has no source/retailer-specific branches.

The method is implemented as `async def` containing `yield`. `AdapterContext` provides an already
configured `httpx.AsyncClient`. The caller owns this client's lifetime. A browser adapter should
open/close Playwright inside its async generator with context managers; install browser extras only
when needed. Store-specific configuration can be injected into the registered adapter factory.
`AdapterContext.run_id` is supplied by the pipeline so acquisition logs can correlate category/page
events with the durable run. Direct parser/adapter tests may leave it unset.

## Required behavior

1. Use a stable lowercase slug for `source_id` and each retailer's `store_id`. Single-retailer sources
   return the same retailer in every candidate. Aggregators preserve the advertised retailer and
   namespace source-generated SKUs so they cannot collide with direct retailer identifiers.
2. Fetch and yield incrementally. Iterate pages/records; do not accumulate the entire catalog.
   MockStore's tiny JSON array is an example, not a catalog-scale loading strategy.
3. Each `AcquisitionItem` contains exactly one canonical candidate mapping or parser error plus
   `SourceEvidence`. The pipeline validates candidates; adapters never bypass validation.
4. Candidate keys must match `Offer`/`StoreProduct`/`Promotion` exactly. Raw retailer fields belong
   only in source evidence bytes or metadata. Do not include SQL entities or matching decisions.
5. Include meaningful SKU/name, explicit price basis, HTTP(S) source URL, and an aware fetch timestamp.
   Prices and quantities must be Decimal, strings, or integers; never binary floats. For JSON
   numeric prices, parse using `json.loads(..., parse_float=Decimal)` or equivalent.
6. `product.quantity` is the total physical contents of one sold item. It may be unknown. Use the
   actual quoted `price_basis` (e.g. 1 kg for variable-weight chicken, 1 package for rice).
   Packaging alone does not establish a fixed weight. Explicit multipacks describe total contents;
   distinguish servings/doses from physical pieces. An unknown-size sale package may have a package
   price basis with unknown contents; record any acquisition inference in source metadata.
   Preserve starting prices as `price_qualifier = from` rather than an exact quote; derived unit
   prices retain that qualifier and cannot be treated as guaranteed checkout costs.
7. Stable `offer_key` distinguishes simultaneous standard, loyalty, coupon, or other selling terms.
   Keep a key stable across a price change; avoid prices, timestamps, or promotion dates in keys.
   `scope` identifies location/channel coverage. Do not merge region-limited prices into national.
8. Convert source promotions into canonical types/conditions. `minimum_purchase` counts multiples
   of the price basis; `current_price` is the per-basis conditional price, not bundle checkout total.
   If a promotion cannot be represented safely, emit a parser error with retained evidence.
   Use `advertised` for an advertised promotion lacking a known price cut. A source percentage
   with an unknown comparison basis uses `discount_reference = unspecified`; never invent a regular
   price. The default reference is `regular_price` and retains strict consistency checks.
   A `price_cut` always requires an actual lower price and a positive regular price.
9. Validate that GTINs identify reusable products before supplying them. Weight/transaction EAN
   codes must not be used for global identity. Missing/ambiguous facts stay unknown.
10. Retain exact source bytes, media type, URL, fetched time, and a record locator (JSON path, CSS
    selector, page number). Metadata may contain source-only facts and parser version. Sanitize
    tokens, cookies, personal data, and signed URLs before retaining evidence. Do not retain request
    authentication headers. A full shared page may be referenced by many items and is stored once.
11. Catch record-level parsing errors and yield them with evidence so other records can continue.
    Raise on source-level problems (HTTP failure, invalid page format, pagination failure). Never
    substitute a successful empty catalog for a source error. Use `raise_for_status()` on HTTP.
12. Keep parsers pure: saved bytes in, candidate/error items out. Resource lifetimes and pagination
    belong to the adapter. Rate limits, robots/source terms, retry behavior and source timezone
    interpretation are adapter responsibilities. Retry only transient requests, with bounded attempts.

## Minimal implementation

```python
class ExampleStore(StoreAdapter):
    @property
    def store_id(self) -> str:
        return "example"

    async def fetch_offers(self, context: AdapterContext) -> AsyncIterator[AcquisitionItem]:
        # parse_page is your pure parser returning evidence-backed candidates/errors.
        response = await context.http.get("https://example.invalid/offers")
        response.raise_for_status()
        for item in parse_page(response.content, fetched_at=utc_now()):
            yield item
```

Register with `registry.register("example", ExampleStore)` in `default_registry`. Add parser fixtures
and a fixture case in `tests/test_adapter_contract.py`. The reusable contract assertion validates
identity, timestamps, raw evidence, field separation, units and promotions. Fixture cases must
exercise the production parser; never stub already-normalized results as the only contract test.
Use `httpx.MockTransport` for HTTP adapters. Contract tests forbid real requests and are offline.
Add malformed records and source-level error tests as well as happy-path cases.

## Failure and history semantics

`Offer.purchase_terms` optionally preserves explicit sale minimums/increments and VAT inclusion.
These quantities use the price-basis unit; physical contents remain on `product.quantity`.
Deposits and mandatory item fees use the same price basis as `current_price`. Missing charges
mean unknown, not zero. `minimum_purchase_cost` and `purchase_cost(required_quantity)` round
up to purchasable quantities and only return an all-in item total when those charges are known.
Estimated weights and unknown basket-level charges must not become guaranteed checkout totals.

The pipeline saves source evidence before validation. Invalid items are persisted as rejected with
diagnostics; they never create product, offer or price rows. `fetched = accepted + rejected` for
completed runs; an operational failure may leave a fetched item incomplete. `changed` counts new
business states, including the first observation. Unchanged items update `last_seen_at` and still
create provenance links. Counts and errors are durable and also emitted as JSON logs.

An absent item is not evidence of unavailability. Emit `unavailable` only when the source says so.
No automatic expiration/deletion is performed. Validity dates are inclusive and do not on their own
prove stock availability; downstream consumers must check both date validity and availability.
