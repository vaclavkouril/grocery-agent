# Website and HTTP clients

With the backend and worker running as described in [backend.md](backend.md), open
`http://127.0.0.1:8001/`. Paste a privately issued bearer session, or accept an administrator's
one-use invitation. Optional password login is shown when enabled. Bearer sessions live only in page
memory; optional secure cookie sessions resume after reloading. Sign out revokes either session. Use HTTPS for remote access; do not
put tokens in URLs, shell history, committed files or screenshots.

The website reads permitted providers, models, cache policies, ingredient names, styles and
request bounds from `/v1/capabilities`. Choose pantry amounts in g/kg or `available`, exclusions,
servings, usage-cost budget and maximum stores. Submit a recipe request, follow progress, cancel
unfinished work, and download the private JSON/HTML report. Offers include observed prices,
retailer/source provenance and freshness warnings. Recent jobs are paginated and owner-only.
Administrators additionally see invitation creation and configured source/profile refresh controls.
Optional nutrition, cooking-time, retailer/loyalty and pantry-priority controls follow the request schema.

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
.venv/bin/pytest -q -s -m browser tests/test_website_browser.py
```

The browser check needs Python Playwright and system Chromium. It uses a temporary loopback
backend, fixture offers, template worker and temporary databases, blocks outside browser requests,
and exercises desktop/mobile forms, pantry amounts, reports, pagination, invitations, cancellation
and account isolation. It saves screenshots under pytest's temporary directory. Browser tests are
excluded from the normal suite. Real retailer/provider/mail/chat checks and Compose execution are
separate operator-configured acceptance runs.
