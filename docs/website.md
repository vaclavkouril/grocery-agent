# Website and HTTP clients

With the backend and worker running as described in [backend.md](backend.md), open
`http://127.0.0.1:8001/`. Paste a privately issued bearer session, or accept an administrator's
one-use invitation. Optional password login and self-registration are shown when enabled.
To display **Create account**, enable both `password_login_enabled` and `registration_enabled`
in the backend configuration; see [registration setup](backend.md#optional-self-registration).
Creating an account signs in immediately as an ordinary user. Bearer sessions live only in page
memory; optional secure cookie sessions resume after reloading. Sign out revokes either session. Use HTTPS for remote access; do not
put tokens in URLs, shell history, committed files or screenshots.

The website reads permitted providers, models, cache policies, ingredient names, styles and
request bounds from `/v1/capabilities`. Choose pantry amounts in g/kg or `available`, exclusions,
servings, usage-cost budget and maximum stores. Submit a recipe request, follow progress, cancel
unfinished work, and download the private JSON/HTML report. Offers include observed prices,
retailer/source provenance and freshness warnings. Recent jobs are paginated and owner-only.
Individual cards also provide [nutrition-app exports](recipe-export.md): manual-entry text
for Kalorické tabulky, ingredients-only paste import and schema.org Recipe JSON. Quantities
are edible raw grams for all servings; the original saved recipe is not changed.
Provider and model selection are visible in the recipe parameters. Local choices include
experimental models with poor benchmark results; selecting one is intentional, not a fallback.
Codex offers only `gpt-6.1-sol` and `gpt-6-luna`, both previously recipe-benchmarked, with
low reasoning effort. The selected model is saved with presets and each generated job.
See the [model comparison](model-benchmark-round-two.md) before choosing; the small benchmark
is not a promise of recipe quality, current availability or weekly usage costs.
Administrators additionally see invitation creation and configured source/profile refresh controls.
Optional nutrition, cooking-time, retailer/loyalty and pantry-priority controls follow the request schema.

The interface uses a responsive cream/forest/apricot theme, system fonts and a local SVG illustration;
it needs no design-service connection, CDN or frontend framework. Meal and pantry controls are
the primary workflow; nutrition and grocery-profile choices use disclosures.
Keyboard navigation includes a skip link, account-tab arrow keys, visible focus and a scrollable
offers region. Touch controls are at least 44px high and reduced-motion preferences are respected.
Sign-up includes password confirmation and inline errors; busy controls prevent repeated submissions.
Saved account pantry is restored after sign-in and changes only through **Save pantry**.
Requests retain their own stock snapshot; generating a recipe never deducts saved inventory.

## Czech and English workspace

The language selector translates navigation, forms, progress, validation and account actions.
The initial interface follows a supported browser language, falling back to Czech. An explicit
language choice is the only browser-stored preference; credentials and pantry stock are never
written to local/session storage. Account language settings synchronize across sessions.
Recipe language is a separate choice and is sent explicitly with new requests. Existing titles,
user content, merchant names and immutable reports are not rewritten when switching the UI.

The workspace separates Recipes, Pantry, Grocery offers, History, Saved recipes and Account; administrators
also see operational refresh and invitation controls. Advanced recipe settings expose ingredients,
nutrition, shopping, generation and grocery data. Server-only currency, location, timezone,
lactose restrictions, ranking, data age and provider limits are displayed rather than editable.
Decimal inputs accept Czech commas or English dots and are sent as exact decimal strings.
Seasoning availability does not imply oil: add oil as a pantry ingredient when owned.

Pantry entry supports ingredient search, quick additions, explicit amounts and units, or an
“enough available” choice. Edit, prioritize or remove rows and save explicitly. Named condiment
checkboxes make the available spices visible separately from weighed ingredients and oil.
Account shopping preferences support several retailers and a maximum of 1–3 shopping contexts;
they initialize recipe and offer controls without overriding explicit request choices.

New website requests ask for three recipe alternatives by default; choose 1–6 before submitting.
Provider usage scales with the count, and each candidate can require a repair call. Partial
failures and duplicate omissions are visible. Each recipe can be bookmarked for later, while
History retains every original request and report. Saved recipes reopen those original results
without calling a model; unbookmarking does not delete history. Purchased ingredients link to
the actual offer source, where available. Owned-only ingredients have no invented shopping link.

Choose a cuisine from Recipes: Greek, Chinese, Vietnamese, Indian, Mexican, Italian, Thai,
Japanese, Korean, French, Spanish, Mediterranean, Czech or Middle Eastern. Cuisine presets
change only that preference; they keep your budget, nutrition constraints, stores and stock.
They do not generate recipes automatically. Choose Codex or a configured Ollama model to
generate cuisine-inspired recipes; fixed templates cannot adapt to a chosen cuisine.
The model must use eligible ingredients and your selected condiments, so local adaptations
may differ from traditional recipes. A cuisine is not an allergy or dietary guarantee.

Save the current recipe preferences directly in Recipes with an editable preset name.
The saved preset is selected for reuse immediately. Account retains the full preset editor
for renaming, updating or deleting selected presets. Presets exclude pantry stock, condiments,
use-first choices and request IDs. History can reuse an owned request with a fresh request ID;
review the current constraints before submitting. Revision conflicts preserve local edits and
ask you to reload instead of silently overwriting another session. Email and SimpleX connections
use the existing verified-binding flow and remain unavailable when disabled on the server.

### Request controls

| Area | API parameters |
| --- | --- |
| Main meal choices | `meal_style`, `cuisine`, `servings`, `max_cost_per_serving_czk`, `max_stores`, `language` |
| Ingredients / pantry | `pantry`, `use_first`, `exclusions`, `condiments`; legacy `seasonings_available` |
| Nutrition / cooking | `min_protein_g`, `max_kcal`, `max_minutes` |
| Shopping | `retailer_ids`, `allow_loyalty` |
| Generation | `provider`, `model`, `recipe_count` |
| Grocery data | `cache_policy`, `source_ids`, single-source `profile_fingerprint` or per-source `profile_fingerprints` |
| Internal identity | `request_id` / submission idempotency key; fresh for a reused request |

Blank optional overrides inherit server settings; loyalty has inherit/yes/no choices.
The condiment inventory is explicit; legacy clients can still send `seasonings_available`.
Models, sources, cache policies and overrides remain server-bounded.
The app never quietly switches a provider or relaxes nutrition, cost or shopping limits.
No-feasible results are distinct from provider failures. Only ingredients with configured
nutrition are eligible; lactose policy is not a guarantee about every allergen or dietary rule.

Offer controls map to `source_id`, `profile_fingerprint`, `scope`, `category`, repeated retailer
filters, `search`, `unit`, `max_unit_price`, `allow_loyalty`, `include_from`,
`include_unavailable` and `sort`. Currency is fixed to CZK; pagination supplies `offset` and
`limit`. Unit-price sorting requires a comparable unit. Freshness and coverage are evidence,
not promises about checkout availability or delivery.

Backend limits come from capabilities: cache-only remains the default, while explicit configuration
can enable recipe-driven refresh and combined compatible sources. Browser controls select sources
and per-source profiles; API submission only queues, and acquisition belongs to the worker.
Administrator refresh requires a worker; failed refreshes retain
eligible prior catalogue data. Model choices are allowlisted, and template jobs remain available
when enabled. Currency and pantry decimals are sent as strings, not JavaScript floating-point values.

## Static application configuration

`src/frontend/index.html` and `src/frontend/assets/` are standalone HTML/CSS/JavaScript modules. There is no
frontend build or dependency installation. The backend serves them at `/` and `/assets`, and the
wheel includes the same files. All API requests are same-origin. Static responses have a restrictive
content security policy; reports are authenticated downloads, not embedded untrusted HTML.

Set `website_enabled = false` in backend TOML, or `GROCERY_BACKEND_WEBSITE_ENABLED=false`, to run
an API-only service. An optional `frontend_dir`/`GROCERY_BACKEND_FRONTEND_DIR` selects a directory
containing `index.html` and `assets/`. Website activation does not enable email or SimpleX.

## CLI delegation

Set `GROCERY_API_TOKEN` privately in the environment, then run:

```sh
.venv/bin/grocery-recipes --api-url http://127.0.0.1:8001 \
  --have rice=500g --budget 80.0000 --servings 2
```

Omitted provider/source/cache choices follow server capabilities. Use `--provider template` for
fixture/local-template execution, `--no-wait` to print the receipt immediately, `--wait-seconds 120`
to bound polling, or `--token-env VARIABLE` to choose a different credential environment variable.
A polling timeout does not cancel the durable job. Without `--api-url`, the CLI keeps its local
recipe-provider path and needs no backend account. Existing `grocery-agent meals` remains unchanged.

## Shared library clients

```python
import os
from grocery_agent.http_client import GroceryClient
from grocery_agent.recipes import RecipeRequest

with GroceryClient("http://127.0.0.1:8001", os.environ["GROCERY_API_TOKEN"]) as client:
    capabilities = client.capabilities()
    request = RecipeRequest(
        provider=capabilities.default_provider,
        cache_policy=capabilities.default_cache_policy,
        pantry={"rice": "500g"},
    )
    receipt = client.submit(request)
    job = client.wait(receipt.job_id)
    if job.status == "succeeded":
        result = client.result(job.job_id)
        html = client.report(job.job_id)
```

`AsyncGroceryClient` exposes the same operations with `async with`/`await`. Both clients offer
typed capabilities, identity, job lists/status and catalogue pages, plus report retrieval,
cancellation and logout. Requests reuse `RecipeRequest.request_id` for idempotency; retries must
reuse that request or an explicit key. HTTP failures raise `BackendError`; credentials are never
placed in report links and redirects are not followed. API contracts are at `/openapi.json`.

## Offline checks

```sh
.venv/bin/pytest -q
npm --prefix src/frontend test
.venv/bin/pytest -q -m browser tests/test_website_browser.py \
  tests/test_website_i18n_browser.py tests/test_website_accessibility_browser.py \
  tests/test_recipe_workspace_browser.py tests/test_cuisine_presets_browser.py
```

The browser check needs Python Playwright and system Chromium. It uses a temporary loopback
backend, fixture offers, template worker and temporary databases, blocks outside browser requests,
and exercises desktop/mobile forms, pantry amounts, reports, pagination, invitations, cancellation
and account isolation. Bilingual tests cover saved stock, revision conflicts, presets, exact
comma-decimal inputs, validation focus, private downloads and request reuse. Accessibility checks
exercise both languages at 320/768/1440 pixels, visible labels, keyboard navigation, reduced motion
and deterministic 200% CSS-zoom reflow. It saves screenshots under pytest's temporary directory. Browser tests are
excluded from the normal suite. Real retailer/provider/mail/chat checks and Compose execution are
separate operator-configured acceptance runs.
