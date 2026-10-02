# Backend and durable worker

The optional backend uses the existing paginated catalogue and a separate control database.
`POST /v1/recipes` validates and persists a job, returns HTTP 202 immediately, and never invokes
a model in the API process. A separately started worker claims jobs and stores immutable JSON
results and HTML reports. The standalone `serve-catalogue` command remains a read-only service;
its capabilities explicitly say recipe submission is unavailable.

## Local quickstart

Install the HTTP extra and refresh grocery data with the collector first:

```sh
.venv/bin/python -m pip install -e '.[catalogue-api]'
.venv/bin/grocery-agent collector --once
.venv/bin/grocery-agent db upgrade control
.venv/bin/grocery-backend bootstrap admin
.venv/bin/grocery-backend --config config/backend.toml serve
# In another terminal:
.venv/bin/grocery-backend --config config/backend.toml worker
```

Bootstrap prints an administrator bearer session once. It can mint another session for an
existing enabled administrator. Store it privately; do not include it in repository files,
URLs or shared logs. Sessions expire after 24 hours by default. This release uses bearer
sessions and invitation acceptance by default. Optional password and cookie sessions are described below. Use TLS
at the reverse proxy for remote access; the default bind is loopback.

Set `GROCERY_SESSION_TOKEN` in your shell to the printed token, then:

```sh
curl -H "Authorization: Bearer $GROCERY_SESSION_TOKEN" http://127.0.0.1:8001/v1/capabilities
curl -H "Authorization: Bearer $GROCERY_SESSION_TOKEN" \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: dinner-1' \
  -d '{"provider":"template","servings":2,"pantry":{"rice":"500g"}}' \
  http://127.0.0.1:8001/v1/recipes
```

Use the returned `job_id` with `/v1/jobs/{job_id}`, `/v1/jobs/{job_id}/result`, or
`/v1/jobs/{job_id}/report`. Reports and job status require the owning account's session;
administrators also cannot read another user's private report. `DELETE /v1/jobs/{job_id}`
cancels queued or running work and fences further completion. An in-flight provider subprocess
may continue until its configured timeout, but cannot publish a cancelled result.

Create an invitation with administrator `POST /v1/invitations` and body
`{"username":"alice"}`. Pass the returned invitation token privately to the recipient.
`POST /v1/invitations/accept` with `{"token":"..."}` creates an invited account
and returns a bearer session. Invitations expire in 24 hours and are consumed once.
`DELETE /v1/session` revokes the current session. Only token hashes are stored.

Open the backend root URL for the [website and client quickstart](website.md). `GET /v1/jobs`
lists only the current user's jobs with `offset` and `limit` pagination. OpenAPI describes bearer
authentication and typed capabilities; public documentation does not grant access to private data.

## Configuration and supported parameters

`--config config/backend.toml` configures API and worker separately from collection. Precedence
is built-in defaults, shared TOML, application TOML, `GROCERY_BACKEND_*` environment, then explicit host/port
CLI options. `GROCERY_CONTROL_DATABASE_URL` selects the control database; `GROCERY_DATABASE_URL`
selects the offer database. Use distinct SQLite files. Startup requires the current control
migration and does not migrate automatically. API and worker must use matching policy settings.

Recipe requests support provider/model, servings, main/breakfast/snack, known-ingredient pantry
quantities in g/kg or `available`, ingredient exclusions, usage-cost budget and maximum stores.
Optional retailer IDs narrow configured retailer permissions. Loyalty permission, minimum protein
(0–300 g), maximum energy (>0–10000 kcal), maximum cooking time (1–480 minutes), owned use-first
ingredients and seasoning availability are shared with the CLI and channel parser.
The configured meal catalogue supplies nutrition thresholds, scope, lactose rules and ingredient
nutrition. The backend validates models against explicit allowlists. An omitted Codex model
uses the installed CLI's configured model; Ollama requires an explicitly permitted local model.
An omitted cache policy resolves to the advertised default (`cache-only` when permitted).
Other policies and combined sources require explicit server configuration; see
[shared recipe execution](recipe-service.md). Optional `profile_fingerprint` selects one published
profile; combined requests use `profile_fingerprints` keyed by source. Cache-only omission retains
the legacy catalogue. See [profile acquisition](acquisition-profiles.md) for coverage semantics.
The unconfigured administrator refresh stays legacy-compatible; explicitly configured refreshes
accept an explicit permitted `profile_fingerprint` or require an unambiguous source profile.
The API advertises these actual limits through `/v1/capabilities` and `/openapi.json`.

Template jobs use the configured fixed recipes with deterministic calculations. Model jobs ask
for titles, raw gram quantities, steps and cooking time, then validate known ingredient IDs and
evaluate the draft through the same Decimal planner. A draft validation error permits one repair;
provider transport failures/timeouts do not trigger a second call. Nutrition and cost are computed
from the pinned registry/quotes, never trusted from model output. No feasible recipe is a successful
job with result status `no-feasible-recipe`, distinct from a failed job.

Codex needs an installed, authenticated CLI on the worker host. The adapter uses stdin, a temporary
directory, a real JSON schema file, read-only sandbox and a bounded timeout, following the
[official non-interactive documentation](https://learn.chatgpt.com/docs/non-interactive-mode).
The container image does not include Codex or its credentials. Ollama defaults to its local HTTP
endpoint and requires a separately configured model service; host loopback does not refer to
another container. Provider and real-retailer acceptance runs remain operator-configured.

## Optional password and browser sessions

Bearer-only operation remains the default. Enable `password_login_enabled` to use
`POST /v1/auth/login` with `{username,password}`; only invited or locally bootstrapped accounts
exist. Accepting an invitation may include an optional password. Bootstrap supports
`--password-stdin`; do not put passwords in command arguments. Passwords contain 15–1024 characters
and at most 4096 UTF-8 bytes. Only versioned salted scrypt hashes are stored. Authentication
errors are generic and durable per-identity/IP throttles apply to public authentication requests.

Enable `cookie_sessions_enabled` and configure an exact `trusted_origin` (for example
`https://grocery.example`) to allow `session_mode: "cookie"`. Cookies are HttpOnly, SameSite=Strict
and Secure by default. Development-only `cookie_secure=false` requires an explicit loopback HTTP
origin. Cookie responses return a CSRF token, never the bearer secret. Every unsafe cookie request,
including logout, requires `X-CSRF-Token`. The website keeps CSRF state in memory and resumes through
safe `GET /v1/auth/session`; no token is written to browser storage. Public
`GET /v1/auth/capabilities` advertises available authentication modes.

## Jobs, recovery and channels

Jobs are deduplicated by `(user_id, Idempotency-Key)`; reusing a key for different parameters returns
409. Without a header, `request_id` supplies the key. Pending jobs are capped per user. Queue states
are queued, running, succeeded, failed and cancelled. Each claim increments the attempt count and
assigns a random lease token; the worker renews it while executing. Expired leases can be claimed
again up to `max_attempts`, after which the job fails visibly. Stale workers cannot renew, pin or
finish. Disabled owners and revoked channel bindings are rechecked before execution/completion.

Before model execution the worker stores effective catalogue/pantry parameters, ingredient context,
eligible quote payloads, observation times, run ID and prompt version in the control DB. Recovery
reuses that pinned snapshot. The completed JSON and HTML are stored with the terminal job update,
so publication is transactional and reports do not use a shared `latest.html`.

Administrator `POST /v1/admin/refresh` accepts `{"source_id":"kupi"}` and a required idempotency
header. Refresh jobs persist the intended acquisition run ID before scraping and take the existing
single-writer lock. After a crash, a successful recorded run is republished; incomplete recorded
acquisitions require explicit administrator resubmission and are never silently replayed. Refresh
requests for the same source/profile by the same administrator coalesce during queued/running work
and the configured cooldown (300 seconds by default). Cross-owner busy/cooldown requests return
generic 409 without another owner's job ID. Each request key remains permanently associated with
its own receipt; recovery verifies the recorded source/profile before republishing.

The [channel applications and API](channels.md) now provide expiring proof-of-control challenges,
verified bindings, confirmed recipe commands, transactional intake deduplication and leased outbox
delivery/recovery. User sessions cannot verify bindings themselves, and separate channel credentials
cannot access user APIs. Email and SimpleX remain disabled until explicitly configured and started.

## Compose and backups

Backend services are opt-in and use the base image's template provider by default:

```sh
docker compose run --rm cli db upgrade control
docker compose run --rm --entrypoint grocery-backend cli bootstrap admin
docker compose --profile backend up -d backend worker
```

These commands are deployment instructions; implementation does not activate services. Worker
shutdown stops accepting jobs and waits for current bounded work. Back up both SQLite databases
with SQLite's backup API or stop writers before copying DB/WAL files, plus offer snapshots and
CLI report artifacts. Control migration `control_0002` preserves users/profiles and adds sessions,
invitations, jobs, bindings and outbox. `control_0003` preserves those rows and adds durable channel
events, confirmations and delivery lease/retry fields. `control_0004` adds durable authentication
throttles, cookie CSRF digests and refresh gates/receipts. Restore reviewed backups rather than
downgrading private data.
