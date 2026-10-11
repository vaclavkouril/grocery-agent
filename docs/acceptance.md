# Remaining acceptance and account setup

## Recipe export verification 2026 10 11

The full offline Python suite passed 1,448 tests. Recipe export fixtures cover exact edible
gram quantities for all servings, pantry stock/oil,
trimming, bilingual pinned labels, historical fallback, arbitrary-precision nutrition totals,
malformed results, ownership and metadata privacy. All 74 Node tests and 21 fixture-backed
Chromium cases passed across regression and focused reruns. The new cases exercise three
download formats, copying and clipboard fallback, stale responses, History/Bookmarks and
session removal on Czech mobile and English desktop. Updated screenshots were reviewed.
Existing workspace selectors were narrowed to distinguish recipe and export disclosures.

Ruff lint/format, mypy on 137 source files and wheel packaging passed. The nine
bundled frontend files and export module match the checkout byte-for-byte. No migration,
model calls, production database changes or third-party account writes are needed.
Kalorické tabulky export is a manual-entry aid; a direct recipe-file import is not verified.
Cronometer Gold/MyFitnessPal ingredient paste and Mealie structured JSON transfer are based
on their official documentation, not live account acceptance. See [transfer instructions](recipe-export.md).

## Model selection verification 2026 10 11

The full offline suite passed 1,339 Python tests and all 69 Node tests. All 19 fixture-backed
Chromium cases passed in batches: 15 existing workspace/accessibility cases and four focused
model/cuisine workflows. Ruff lint/format, mypy on 136 source files and wheel packaging passed;
the nine bundled frontend files, backend configuration and model choices match the checkout.

The picker exposes every configured local model, including weak benchmark performers, and
retains explicit choices across provider/language changes, preset reuse and job submissions.
Codex requests pin an explicit model from the two cheap tested IDs; untested configuration,
unsupported requests and empty-allowlist CLI fallbacks are rejected. Provider fixtures verify
low reasoning effort. Czech mobile and desktop screenshots were visually reviewed, including
the full-width mobile model control. No provider calls, downloads or weekly quota were used.
Ollama was stopped during these checks; start it on the worker host for local inference.
This feature needs no new migration beyond the existing `control_0006` requirement.

## Cuisine presets and quick saving verification 2026 10 09

The full offline suite passed 1,312 Python tests. All 67 Node tests and 17 fixture-backed
Chromium browser cases passed, including Czech mobile and English desktop cuisine workflows.
Ruff lint/format, mypy on 135 source files and wheel packaging passed.

Cuisine fixtures cover all 14 canonical IDs, provider prompts and local adaptations, CLI/API/text
parity, immutable pinned preferences, invalid choices and rejection of fixed-template cuisine
requests before acquisition or provider calls. Browser checks cover applying every cuisine shortcut
without changing budget, nutrition, stock or shopping settings; keyboard quick-save; blank-name
validation; automatic saved-preset selection; reuse, updates and deletion; and keeping saved names
literal when changing interface language. Duplicate submissions and edits during saving have
Node regression coverage. Both mobile and desktop screenshots were visually reviewed.

Cuisine selection uses existing JSON request/preset storage and needs no migration beyond
`control_0006`. Fixed templates remain unchanged. The checks use queued jobs and fake providers;
no live models, retailers, accounts or production databases were used for this feature.

## Pantry, multi-store and recipe-library verification (2026-10-09)

The full offline suite passed 1,197 Python tests; 64 Node client/interaction checks also passed.
Ruff lint/format checks, mypy on 134 source files and wheel packaging passed. All nine packaged
website files were checked byte-for-byte against source; the wheel includes `control_0006`.

New fixtures cover named condiment persistence and omission compatibility, explicit shopping
overrides, batch snapshot pinning, duplicate omission, bounded repair and transport failures,
private bookmarks, concurrent bookmark deduplication/quota, CSRF and migration preservation.
All 15 real-Chromium browser checks passed on the final full run. Browser workflows cover visible
pantry amounts/units, revision conflicts, multi-retailer searches,
source URLs, recipe retention during a new request, bookmarks and bilingual batch warnings.
Accessibility checks cover both languages at 320/768/1440px, keyboard operation and reduced motion.
A late sign-in focus race was fixed and covered by two delayed-authentication Node regressions.

No production databases were migrated, no providers or retailers were called, and no external
accounts or plugins were changed for this feature. Back up control storage and run
`.venv/bin/grocery-agent db upgrade control` before restarting the updated API and worker.
Offers remain at `offers_0003`; existing requests and immutable reports are retained.

## Website and self-registration verification (2026-10-02)

The redesigned responsive website and opt-in self-registration passed 1,029 offline tests
and all four real-Chromium browser tests, plus 31 Node client/interaction checks. Ruff lint/format,
mypy (126 source files), diff checks and wheel packaging passed. The wheel includes the final
HTML, CSS, JavaScript and configuration; no external font/design service is required at runtime.

Browser checks covered desktop/390px mobile registration, confirmation mismatch without a request,
duplicate usernames, ordinary-user roles, cookie resume/revocation, private account/job isolation,
keyboard tabs/skip-link focus, existing recipe/report/offer workflows and horizontal overflow.
Screenshots for visual review are under `/tmp/grocery-ui-{desktop,mobile}.png` and
`/tmp/grocery-ui-registration-{desktop,mobile}.png`; these are temporary, not deployable assets.
Passwords and session secrets are cleared from form fields and never persisted in browser storage.
API tests additionally cover atomic concurrent registration, password bounds, rate limiting before
hashing, and bounded password-work concurrency.

Registration is implemented but deliberately not enabled in existing deployments. Enable both
`password_login_enabled` and `registration_enabled` after bootstrapping an administrator; see
[backend registration setup](backend.md#optional-self-registration). There is no email verification
or password-recovery feature. Figma was discovered and suggested for editable design collaboration,
but was not connected or used to generate these assets. No plugins or external accounts were modified.
Live-model/retailer/mail/chat and Docker checks were not repeated for this change.

## Planner refactor verification (2026-10-02)

The pre-refactor offline/browser suite passed all 1,013 tests. The planner now prepares
ingredient regexes once per request, normalizes each eligible offer's searchable text once,
serializes offers only when all cheaper comparison keys tie, and streams complete baskets
instead of retaining every combination. Decimal arithmetic and the legacy tie-break order
remain unchanged; no NumPy dependency or floating-point financial calculations were added.

A local Python 3.14 synthetic benchmark used the six offers from `tests.test_meals.groceries`,
repeated 1,000 times with unique SKU prefixes and price offsets cycling through 0–6 CZK.
Five in-memory planning calls had median elapsed times of 1.684s before and 1.012s after
(about 40% less time). Report JSON was byte-identical. This measures planning for that
6,000-offer fixture, not scraping, provider inference, API latency or every catalogue size.
Ordering parity, avoiding unnecessary serialization, and streamed combination ordering
are covered by `tests/test_planner_optimization.py` without timing-sensitive assertions.
After the refactor, all 1,016 offline/browser tests passed (two live tests deselected),
along with Ruff lint/format checks, mypy on 126 source files, the frontend Node contract
test, and wheel packaging. The upstream TestClient deprecation warning remains.
Live-provider, retailer and deferred account checks were not repeated for this planner-only change.

## Earlier integration results (2026-10-02)

The final full offline/browser suite passed (1,013 tests), with two opt-in live tests deselected.
Ruff, mypy (126 files), Node contracts and wheel packaging passed. One upstream FastAPI/httpx
TestClient deprecation warning remains; it is not a test failure. Real Codex generation exposed an unsupported
Decimal regex in the provider schema; it was fixed and the real structured-output check passed.
Real Ollama generation passed with the installed `qwen2.5-coder:0.5b` after constraining ingredient
IDs in the schema. The 1.5b model exceeded the initial 60-second smoke limit, then passed with the
fixed schema and the normal 120-second timeout (71 seconds). Live Ollama tests now use that
application default; both installed qwen model sizes passed.
Live checks are explicit and excluded from the normal suite:

```sh
GROCERY_LIVE_CODEX=1 .venv/bin/pytest -q -m live tests/test_live_providers.py -k codex
GROCERY_LIVE_OLLAMA_MODEL=qwen2.5-coder:0.5b .venv/bin/pytest -q -m live tests/test_live_providers.py -k ollama
```

Codex uses the existing CLI login/configured model unless `GROCERY_LIVE_CODEX_MODEL` is set,
following the [official non-interactive interface](https://learn.chatgpt.com/docs/non-interactive-mode).
These checks send synthetic ingredient context, not private pantry or customer data.

## Public acquisition results

Produce-only native profiles were collected and published using temporary offer databases and
evidence under `/tmp/grocery-public-acceptance-WIwU3i`, not production storage:

| Source | Result | Scope/coverage |
| --- | --- | --- |
| Kupi | 219 offers accepted, 0 rejected/errors; 9 pages | Praha, `ovoce-a-zelenina` only; complete selected profile |
| Rohlík | 753 offers accepted, 0 rejected/errors; 713 products | Anonymous warehouse 8799 / Praha 8, category 300102000 only; complete selected profile |
| Tesco | Failed, 0 fetched/accepted; HTTP 403 fetching robots.txt | Produce page not requested; no bypass attempted |

Successful fingerprints: Kupi `22492af7eaba6a8f3a83cf76`, Rohlík `641ab368c27fad0f14696d32`.
These are not full grocery-category or delivery-address acceptance claims. Tesco access denial is
an external blocker, not a parser fix. Its diagnostic used temporary storage under
`/tmp/grocery-tesco-live-xz65fs`. Temporary captures may be discarded later; do not treat them as backups.

## Docker prerequisite requiring interactive sudo

Docker is installed, but its daemon is stopped and Compose is missing. Automated setup was
approved but could not proceed because sudo requires a password. On this Arch host run:

```sh
sudo pacman -S --needed docker-compose
sudo systemctl start docker
docker compose version
```

Then run the CI container smoke commands from `.github/workflows/ci.yml` against an isolated
project name, for example `docker compose -p grocery-acceptance ...`. This avoids the normal
deployment's named data volume. Do not run `down --volumes` against production. If Docker socket
access is restricted, run Docker commands with interactive sudo rather than weakening permissions.
Installing/starting Docker is not the same as enabling grocery production services.
Container image/Compose execution (including the Python 3.13 image) and optional live PostgreSQL
verification remain unrun. The local verified Python is 3.14; SQLite remains the default storage.
Ollama is reachable on loopback with existing local models; no model download was required.

## Where to add the deferred live account configuration

Keep secrets in the service/process environment, never TOML, Git or command arguments.
The channel applications do not automatically load `.env`; export credentials or use a protected
service EnvironmentFile. `.env.example` lists names only. Keep email/SimpleX disabled until ready.

- Email nonsecrets: `config/email.toml` (hosts, sender, trusted ingress/senders, backend URL,
  enabled intake/delivery). Backend switch: `config/backend.toml` → `email_enabled`.
  Environment: `GROCERY_EMAIL_IMAP_USERNAME`, `GROCERY_EMAIL_IMAP_PASSWORD`,
  `GROCERY_EMAIL_SMTP_USERNAME`, `GROCERY_EMAIL_SMTP_PASSWORD`, `GROCERY_EMAIL_SERVICE_TOKEN`.
  The service token must match `GROCERY_BACKEND_EMAIL_TRANSPORT_TOKEN` in the backend environment.
  Use a dedicated test mailbox and explicitly trusted ingress; see [email](email.md).
- SimpleX nonsecrets: `config/simplex.toml` → daemon `ws_url`, `user_id`, paired
  `allowed_contact_ids`, backend URL and `enabled`. Backend switch: `simplex_enabled`.
  Environment: `GROCERY_SIMPLEX_TRANSPORT_TOKEN`, matching
  `GROCERY_BACKEND_SIMPLEX_TRANSPORT_TOKEN`. Pair a designated test contact manually and verify
  the active profile before testing. No pairing/contact creation is automatic. See [SimpleX](simplex.md).
- Makro: create private browser state with `python -m grocery_agent.stores.makro.login`.
  Set `GROCERY_MAKRO_SESSION_PATH` to the resulting private file, plus a distinct
  `GROCERY_MAKRO_ACCOUNT_SCOPE` and exact `GROCERY_MAKRO_STORE_NAME`. Keep the session outside
  source/Git; the default is ignored `data/makro/session.json`. See [Makro setup](makro.md).

After configuration, migrate separate test databases, start a test backend/worker, prove channel
bindings, and run one confirmed recipe through the test mailbox/contact. Check receipt deduplication,
private report delivery and reconnect/retry behavior. Actual sending changes external state; use only
designated test recipients. Real customer credentials and production DBs are not needed for regressions.
