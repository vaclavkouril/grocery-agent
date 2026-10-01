# Rohlík public catalog fixtures

Captured on 2026-10-01 from anonymous HTTPS requests to www.rohlik.cz.

- `robots.txt`: the unmodified public robots policy.
- `context.html`: reduced `__NEXT_DATA__` state from `/`, keeping only public navigation,
  anonymous warehouse identity and displayed delivery locality. Cart, session, advertising,
  company, client and other unrelated state were removed.
- `cards.json`: eight unmodified product-card objects selected from
  `/api/v1/products/card?products=<100 produce product IDs>&categoryType=normal`.
  The original response was a JSON array; the subset is pretty-printed for review.
  It covers package/piece goods, approximate variable weights, ordinary sales and conditional
  “pro vás” prices. No credentials or customer/address information is included.

`tests/rohlik_support.py` supplies bounded two-page category ID/count responses around this
subset. These pagination envelopes are synthetic; the card/context parsers use captured data.
Additional parser tests mutate these cards to exercise pack sizes, stock and malformed inputs.
