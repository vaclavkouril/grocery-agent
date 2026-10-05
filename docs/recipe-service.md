# Local-first and combined-source recipes

`grocery-recipes` now uses the same reusable `RecipeService` as backend workers. The CLI works
without accounts or a server; `--api-url` explicitly delegates to the authenticated backend.
The default local request uses Codex and local-first grocery data. Existing `grocery-agent meals`
commands retain fixed-template behavior. Grocery data is cached, not generated recipes.

```sh
.venv/bin/grocery-recipes --provider template --cache-only --profile-fingerprint FINGERPRINT
.venv/bin/grocery-recipes --config config/recipes.toml --have rice=500g --budget 80
.venv/bin/grocery-recipes --no-cache --output-json /tmp/my-recipes.json --output-html /tmp/my-recipes.html
```

Replace `FINGERPRINT` with a published value from acquisition output or `/v1/collections`.
The second command may acquire grocery data; the third requires a successful new collection.
Use `--cache-only` to guarantee no scraping. Both cache flags are mutually exclusive with
`--cache-policy`. Ollama requires an explicit local model; no model service is installed or started.

## Cache policies

| Policy | Behavior |
| --- | --- |
| `local` | Reuse a fresh, complete selected collection; refresh missing/stale profiles. |
| `cache-only` | Never acquire; missing/stale selections fail. Single-source legacy reads remain compatible. |
| `refresh` | Attempt a new collection; a failed refresh may use the same profile's fresh complete prior collection, with warnings. |
| `no-cache` | Require successful publication of a new complete collection; never fall back. |

All acquisitions share the writer lock with scrapers, collectors and administrator refreshes.
Recipe requests wait at most 30 seconds for this lock, then recheck freshness inside it.
Simultaneous local-first misses can reuse the first completed refresh rather than duplicate it.
Other commands retain their existing nonblocking lock behavior. Failures never extend freshness
deadlines or substitute another profile. Declared partial profiles are not eligible for recipe refresh.

For `cache-only`, an omitted fingerprint retains the legacy head. Its coverage is explicitly unknown;
it cannot satisfy a combined-source request. For other policies, omission selects a unique configured
`default` profile; local CLI defaults can resolve the source adapter's built-in profile. An explicit
fingerprint can use an already fresh collection in local mode even when refresh is not configured,
but a miss then fails instead of guessing acquisition parameters. Freshness is bounded by both
catalogue and meal policy limits.

## Configuration and compatible sources

`config/recipes.toml` configures the local application. Precedence is built-in defaults → recipe
TOML → `GROCERY_RECIPES_*` environment → CLI request overrides. Shared database, snapshot,
meal-registry and report paths still use `GROCERY_*`. There is no automatic shared TOML layering.

To combine sources, explicitly configure their evidence scopes as compatible. This is an operator
decision, not an inferred geographical equivalence. For example, these identifiers describe Praha
advertisements and Tesco anonymous online quotes, not guaranteed identical delivery/store pricing:

```toml
default_provider = "codex"
default_cache_policy = "local"

[source_scopes]
kupi = ["kupi:locality:praha"]
tesco = ["tesco:online:anonymous"]

[[acquisition_profiles]]
source_id = "kupi"
name = "default"

[[acquisition_profiles]]
source_id = "tesco"
name = "default"
```

Acquisition parameters follow the [profile contract](acquisition-profiles.md), including native
categories, source options and quote filters. Adapter dependencies and credentials must be configured
separately; adding a profile does not install a browser or authenticate an account.

```sh
.venv/bin/grocery-recipes --config config/recipes.toml --source kupi --source tesco
.venv/bin/grocery-recipes --config config/recipes.toml --cache-only \
  --source kupi --source tesco --profile kupi=KUPI_FINGERPRINT --profile tesco=TESCO_FINGERPRINT
```

Replace both fingerprint placeholders with published values. One to four unique sources are accepted.
Each selected source contributes one pinned run and fingerprint. Original quote scopes, retailer IDs,
URLs, observation IDs and acquisition source/run provenance remain intact. Store limits count distinct
`source|retailer|scope` shopping contexts, so two unrelated contexts with the same retailer name are
not silently merged. Reports retain both retailer labels and precise contexts.
Pantry deductions, nutrition, usage costs, ranking and filters still use deterministic Decimal math.
Costs are consumed-ingredient estimates, not complete checkout totals or cross-store basket guarantees.

## Backend and clients

The backend remains cache-only by default. To allow recipe-driven acquisition, explicitly set
`cache_policies`, `source_scopes` and `acquisition_profiles` in its separate TOML or environment.
Use the same acquisition selectors/environment in API and worker processes so fingerprints agree.
For Compose, the mounted backend TOML configures these new fields; `.env` values are not
automatically forwarded unless they are included in a service's environment configuration.
`/v1/capabilities` advertises allowed policies, compatible source scopes and combined-source support.
API submission validates and queues requests; it does not scrape. The worker acquires when necessary,
then pins immutable inputs before model execution. Recovery reuses those pinned inputs.
Normal users may request permitted recipe cache policies; standalone refresh remains administrator-only.
The original unconfigured administrator refresh still updates the legacy head. When profiles are
explicitly configured, standalone refresh accepts a permitted fingerprint or requires an unambiguous
configured profile. Matching owned requests coalesce and share cooldown state; other owners do not
receive private receipt IDs. Capabilities expose `refresh_profiles`.

JSON requests use `source_ids` and `profile_fingerprints` keyed by source. The scalar
`profile_fingerprint` remains supported for single-source requests, but cannot be combined with the map.
Shared email/SimpleX grammar accepts, for example:

```text
recipe source=kupi,tesco profiles=kupi:KUPI_FINGERPRINT,tesco:TESCO_FINGERPRINT cache_policy=cache-only
```

Replace placeholders as above. Text commands remain subject to advertised capabilities and existing
confirmation/ownership rules. The website offers source and per-source profile controls from those
same capabilities. All transports retain provider/model allowlists and per-user quotas.

## Library use and immutable reports

```python
from pathlib import Path
from grocery_agent import RecipeRequest, RecipeService, RecipeSettings
from grocery_agent.config import Settings

options = RecipeSettings.load(Path("config/recipes.toml"))
service = RecipeService(Settings(), options)
request = RecipeRequest(provider="template", cache_policy="cache-only")
inputs = service.prepare(request)
result, html = service.execute(request, inputs)
```

`prepare` is synchronous and may acquire under an
acquisition-enabled policy; call it outside an event loop or through a worker thread. `execute` reads
only the supplied pinned snapshot. `run` combines the two. Provider and refresh boundaries are injectable;
`RecipeCache` also accepts a source registry for fixture/custom-adapter acquisition.

The CLI saves `request.json`, `inputs.json`, `report.json` and `report.html` under
`data/reports/requests/REQUEST_ID`. Existing request directories are never overwritten, and explicit
output copies must be outside that immutable tree. Backend results remain private and transactionally
stored in the separate control database. Inputs include effective parameters, nutrition context, all
selected collections, provenance and prompt version. Later collections cannot alter persisted results.

Current migration heads are `offers_0003` and `control_0005`; production storage is never migrated
automatically. Live model, retailer and container acceptance runs remain separately configured.
Shared TOML defaults and portable installed application startup are documented in
[configuration](configuration.md). The optional password/cookie session flow is in [backend](backend.md).

Additional request controls are `retailer_ids`, `allow_loyalty`, `min_protein_g`, `max_kcal`,
`max_minutes`, `use_first` and `seasonings_available`. CLI equivalents are `--retailer` (repeatable),
`--allow-loyalty`/`--no-loyalty`, `--min-protein`, `--max-kcal`, `--max-minutes`, `--use-first`
and `--have-seasonings`. Nutrition/time constraints are evaluated deterministically; an otherwise
valid but infeasible draft does not trigger repair. Retailers can only narrow a configured allowlist,
and use-first ingredients must exist in known owned pantry stock.

`RecipeRequest.language` accepts `cs` or `en`, defaulting to `en` for existing callers.
Use `grocery-recipes --language cs`, or `recipe language=cs` in a channel command. New requests
pin the language and `recipe-draft-v2` prompt version in their ingredient snapshot. Models receive
the requested language for titles and preparation steps; template recipes use configured
`titles` and `translated_steps`. Ingredient `labels` may provide Czech and English display names,
with the existing `label` as fallback. Report headings and amounts follow the request language;
changing the website language does not translate or regenerate historical reports.

## Local model benchmarking

The local-only benchmark runs model-written recipes against pinned cached offers and the same
recipe service and Decimal evaluator. It records raw responses, request and token-processing
times, repairs, rejections, and model memory metadata. It never acquires or migrates grocery data.
`--inputs-from` replays historical cases without rereading a changing cache. See the
[machine-specific comparison and reproduction commands](local-model-benchmark.md).
The [second round](model-benchmark-round-two.md) compares five more local models with budget
Codex models and documents token consumption, quota limitations, and cost estimates.
