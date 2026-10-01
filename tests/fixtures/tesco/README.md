# Tesco Online Nákupy public fixtures

Captured with an ordinary visible Chromium browser on 2026-10-01.

- `robots.txt`: unchanged live public robots response.
- `produce.html`: the captured server response from
  `https://nakup.itesco.cz/shop/cs-CZ/browse/ovoce-a-zelenina/all`, reduced by the production
  `public_snapshot` function. Product markup, navigation and coverage are retained; session,
  tracking, cart, account and unrelated script data, SVGs and images are removed. BeautifulSoup
  serializes the retained markup. A marker identifies this reduced public snapshot.
  The source contains 24 genuine products, four Clubcard variants, standard prices, ordinary
  cuts and loose-produce price bases; it declares 393 products in the complete department.

`tests/tesco_support.py` splits these same product tiles into two twelve-product test pages,
changing only the displayed counts, layout membership and pagination links. These coverage
wrappers are synthetic. The production parsers operate on the captured product markup.

The actual live next-page request was denied with HTTP 403, so these fixtures do not claim to
be a complete live Tesco catalog. No authentication cookies or account details are included.
