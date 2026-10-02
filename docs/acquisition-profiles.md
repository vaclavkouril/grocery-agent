# Profile-aware acquisition and cache

`grocery-scrape` and `grocery-collect` are separate, profile-aware applications.
They use the shared scraper pipeline, price history, evidence store and catalogue publisher.
They do not require accounts, recipe providers or a running backend. No service starts merely
because its configuration exists.

```sh
.venv/bin/grocery-scrape --config config/scrape.toml --profile kupi:default
.venv/bin/grocery-collect --config config/collect.toml --once
.venv/bin/grocery-collect --config config/collect.toml --daily-at 02:00 --timezone Europe/Prague
```

These commands acquire real data when invoked. Use absolute paths for scheduled processes.
The collector defaults to Kupi, 02:00 Europe/Prague, with an initial collection on startup.
`--source` and `--profile` are repeatable; ambiguous names require `source:name`.
`--source SOURCE` selects its configured `default` profile or creates a default for that source.
An unsuccessful round returns nonzero and emits safe per-profile JSON errors.

The existing Docker collector retains its legacy command. Optional `profile-scrape` and
`profile-collector` roles use the new apps and shared persistent volume:

```sh
docker compose run --rm profile-scrape
docker compose run --rm profile-collector --config /app/config/collect.toml --once
```

Use `docker compose --profile acquisition up -d profile-collector` only when intentionally
activating scheduling. The common writer lock serializes acquisition; avoid scheduling duplicate
legacy/profile collection unnecessarily. Compose mounts `./config` read-only into the applications;
the standalone image also includes build-time configuration copies.

## Configuration and selectors

Shared paths and HTTP settings still use `GROCERY_*`; native adapter settings use their existing
source-specific environment prefixes. Each new application has its own TOML file. Precedence is
built-in defaults → application TOML → `.env` → process environment → explicit CLI overrides.
`GROCERY_SCRAPE_PROFILES` and `GROCERY_COLLECT_PROFILES` accept JSON profile arrays.
Collector scheduling uses `GROCERY_COLLECT_DAILY_AT`, `GROCERY_COLLECT_TIMEZONE` and
`GROCERY_COLLECT_RUN_ON_STARTUP`. `--config` chooses the application file; there is no automatic
shared TOML layer in this release. Existing `GROCERY_COLLECTOR_*` settings belong to the legacy app.

For example, replace the profiles in either new application configuration with:

```toml
[[profiles]]
source_id = "kupi"
name = "produce"
categories = ["ovoce-a-zelenina"]
retailer_ids = ["billa"]
eligibility = ["exact", "non-loyalty"]

[[profiles]]
source_id = "kupi"
name = "meat"
categories = ["maso-drubez-a-ryby"]
```

Categories are native source selectors, not translated product labels: Kupi/Tesco slugs,
Rohlík decimal category IDs encoded as strings, or Makro category paths. `search` is an array
of phrases, all of which must match the normalized product name/brand. `retailer_ids` is an
inclusive list. Eligibility rules (`exact`, `available`, `non-loyalty`) are conjunctive.
`scope`, when present, must match the original quote scope exactly. Scope names are source
evidence, not a universal location mapping; quotes are never relabeled to match configuration.

Profiles resolve against the configured adapter before storage opens. Safe native `options`
include category selectors, page limits and source-specific warehouse/account/branch selectors.
Unsupported options are rejected. Fingerprints include effective acquisition parameters and
filters, but exclude credentials, cookie/session paths, browser binaries, timing and retry settings.
Changing a profile name or effective parameters creates a different fingerprint; obsolete heads
are retained, not automatically garbage-collected. Credentials stay in environment/session storage.

## Publication, migration and failures

`offers_0003` adds per-run metadata and heads keyed by `(source, profile_fingerprint)`.
Writer composition applies this migration; a read-only API never creates or upgrades storage.
Before upgrading persistent data, back up the offer database and snapshots as described in
[catalogue operations](catalogue.md). This implementation did not migrate a production database.
The migration preserves observations, snapshots and existing cache membership. Historical runs
and heads are labeled `legacy`, with unknown/incomplete coverage rather than invented profiles.

Only successful, finished, nonempty collections with complete declared/observed coverage and
consistent actual scopes are published to an explicit profile. `coverage = "partial"` retains
history but cannot replace a head. Publication is atomic, refuses older runs and keeps other
profiles' indexes. The acquisition writer lock remains shared across applications.
Failed refreshes retain the prior collection for the same profile within its freshness deadline,
with warnings. They do not degrade another profile. Stale data is refused, not silently extended.
Actual scopes are derived from accepted canonical quotes; invalid candidates still reach the
pipeline and prevent successful publication instead of disappearing during filtering.

Legacy `grocery-agent scrape`, workflow and collector commands retain legacy publication and
fixed-recipe behavior. An omitted fingerprint reads only `legacy`, never guesses a named profile.
New applications publish named fingerprints, so select them explicitly when consuming their data.

## API and consumers

`GET /v1/collections?source=kupi` lists published fingerprints, profile parameters and coverage,
including stale heads. Listing is not a freshness guarantee. Status, readiness and offers accept
`profile_fingerprint`; missing/stale explicit selections return unavailable rather than fall back.
The authenticated backend inherits these routes and its existing authorization checks.

```sh
# Replace FINGERPRINT with the 24-character value from acquisition output or /v1/collections.
.venv/bin/grocery-recipes --api-url http://127.0.0.1:8000 \
  --profile-fingerprint FINGERPRINT
```

Set `GROCERY_API_TOKEN` privately. JSON requests use `profile_fingerprint`; shared email/SimpleX
commands accept `profile=FINGERPRINT`. The website lists saved profiles for offers and recipes.
Backend jobs persist the selected run, fingerprint, actual scopes, coverage, quotes and nutrition,
so later refreshes cannot change a queued request or report. The shared Python HTTP clients expose
`collections()` and profile-aware `CatalogueQuery` filtering.

Local-cache-first recipe execution and explicitly configured combined sources now use the shared
[recipe service](recipe-service.md). Local profile selection no longer requires `--api-url`.
Backend policies remain cache-only unless explicitly enabled. Administrator refresh retains its
legacy default, or requires an unambiguous explicitly configured profile; choosing among multiple
profiles in that standalone endpoint remains follow-on work.
