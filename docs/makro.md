# Makro direct assortment

Makro is registered as `makro`. The browser adapter visits ordinary categories at
`https://sortiment.makro.cz/shop/category/…`, follows the site's load-more control,
and visits selectable packaging alternatives. It checks counts, unique product variants,
unchanged branch and consistent prices before declaring a run complete.
It respects robots.txt for catalog navigation and does not directly request restricted backend
or search endpoints. Product cards become the retained evidence; account headers, session
state, scripts, customer-specific identifiers and tracking attributes are excluded.

## Login and branch

Makro's [new assortment notice](https://www.makro.cz/info-a-sluzby/informace-pro-zakazniky-online-sortiment)
says that prices require login and correspond to the customer's registration.
Create a Makro account first, install the browser extra, then run the local setup command:

```sh
.venv/bin/python -m pip install -e '.[browser]'
.venv/bin/python -m grocery_agent.stores.makro.login
```

In the Chromium window, sign in and select your branch. Press Enter in the terminal.
The command checks that supported VAT-inclusive prices are visible, then saves browser state
to `data/makro/session.json` with private file permissions. `data/` is ignored by Git.
The setup requires a graphical desktop. Subsequent collection can run headlessly:

```sh
.venv/bin/grocery-agent scrape makro
```

Optional `.env` settings:

```dotenv
GROCERY_MAKRO_SESSION_PATH=data/makro/session.json
GROCERY_MAKRO_ACCOUNT_SCOPE=local
# Exact displayed name; catches an accidentally changed branch.
GROCERY_MAKRO_STORE_NAME=makro Praha - Stodůlky
# Omit to collect both food and non-food roots.
GROCERY_MAKRO_CATEGORY_PATHS=["potraviny"]
GROCERY_MAKRO_MAX_PAGES=1000
GROCERY_MAKRO_REQUEST_DELAY_SECONDS=1
GROCERY_MAKRO_BROWSER_HEADLESS=true
# GROCERY_MAKRO_BROWSER_EXECUTABLE=/usr/bin/chromium
```

Use a distinct account alias and session file for each customer account. Change the alias when
switching accounts. Prices have scopes such as
`makro:in-store:branch:makro-praha-stodulky:account:local`. The captured public catalog showed
“Nakoupíte v obchodě”; this adapter records branch assortment and does not assert that every
item can be ordered for delivery or collection. Missing/expired login fails the run visibly.

## Minimum purchase and taxes

Makro [explains that its main prices exclude VAT](https://help.makro.cz/cs/support/solutions/articles/80001218484-ceny-v-makru-jsou-uvedeny-bez-dph-pro%C4%8D-makro-uv%C3%A1d%C3%AD-ceny-bez-dph-).
The parser requires an explicit `vč. DPH` selling price. It never guesses a VAT rate from
the product category. The UI's selected-bundle gross price becomes `current_price` for one
whole sold package. A case's inner-piece quote is not its minimum purchase cost.
The full physical contents determine price per kg/l/piece, including nested multipacks.

Canonical offers now optionally carry `purchase_terms`: VAT inclusion, explicit sale minimum
and increment, deposit and mandatory item fee per quoted price basis, membership and conditions.
`offer.minimum_purchase_cost` exposes the minimum package quantity, VAT-inclusive merchandise
subtotal, deposit, fee and total. `offer.purchase_cost(required_quantity)` rounds demand up to
the allowed sale quantity. The catalogue's serialized offers include these fields; existing
unit-price sorting continues to compare merchandise per unit, rather than checkout totals.
The meal planner still estimates consumed-ingredient costs; it does not rank recipes by
whole-package checkout cost or aggregate basket fees.

For example, a **synthetic test case**, six 500 g packs costing 112 Kč including VAT have a
3 kg sale quantity and a 112 Kč minimum merchandise cost. Needing 4 kg requires two cases,
224 Kč. Refundable deposits still increase the amount due at checkout.

Unknown deposits or fees remain `null`; the all-in item total is then `null` even when the
merchandise subtotal is known. A quote with “cca 8 kg” retains its exact per-kg price but has
no guaranteed minimum checkout amount. Basket-level service/delivery charges, account eligibility
and minimum order values require checkout information and are not inferred from product cards.

## Verification limits

On 2026-10-01, the live anonymous food root displayed 24 of 17,601 products, packaging,
branch availability and a login prompt instead of prices. Public pack selection was verified,
including a six-can bundle and a 24-can `4 × (6 × 500 ml)` bundle. No customer login was supplied,
so authenticated prices and complete collection remain unverified.

Offline tests use the sanitized captured anonymous page, plus explicitly synthetic prices on
that card structure. The price selectors/labels were inspected in the publicly served Makro UI
and Czech translation files. Authenticated markup must still be checked after account setup.
Missing gross prices, ambiguous packages and unverified volume/conditional discounts are rejected;
any such rejection fails completeness rather than publishing a misleading full catalog.

If the logged-in UI exposes additional price or deposit layouts, retain a sanitized example
and extend the parser and tests before using that layout for comparisons.
