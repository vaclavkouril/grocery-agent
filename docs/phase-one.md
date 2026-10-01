# Application foundation

Phase 1 supplies account-independent services, validated commands, independent migrations and
isolated request reports. The requested collector/catalogue/Docker extension is implemented here.
Phase 1b adds optional breakfast and snack styles while keeping main dishes as the default.
The remaining order is phase 2 users/jobs, phase 3 browser, phase 4 email, phase 5 SimpleX.

## Modules and interfaces

| Module | Boundary |
| --- | --- |
| `application/commands.py` | Version 1 `MealCommand`, `WorkflowCommand`, `ScrapeCommand`; bounded text parser |
| `application/parameters.py` | Defaults, optional saved parameters, request overrides; shared pantry parsing |
| `application/services.py` | `AcquisitionService.acquire`, `MealService.plan`, `WorkflowService.run`; typed results |
| `application/runtime.py` | Local HTTP/SQL resource composition, independent of accounts |
| `application/reports.py` | Immutable per-request directories and optional local `latest` aliases |
| `catalogue/` | Publication, expiry-aware indexed queries and optional private read-only HTTP app |
| `collector/` | Independent one-off or daily acquisition process |
| `persistence/control/` | Optional separate user/profile schema and owner-filtered profile repository |
| `persistence/migrations/` | Separate Alembic histories for offers and control storage |

Services return typed objects and never print, send messages or authenticate users. The CLI formats
results and coordinates local writer locks. Future channel handlers will call services directly.
The optional control repository receives an owner ID from its caller; phase 2 must authenticate and
authorize that identity. Phase 1 introduces no usable login, sessions, job queue or public browser UI.

## Typed commands and shared parameters

```python
from grocery_agent.application.commands import MealCommand, parse_command
from grocery_agent.application.parameters import MealOverrides, PantryOverrides, parse_owned_stock

command = MealCommand(
    overrides=MealOverrides(
        pantry=PantryOverrides(
            items=parse_owned_stock(["chicken=2kg", "rice=5kg"]),
            use_first=("chicken",),
            seasonings_available=True,
        )
    )
)
same_boundary = parse_command(
    "meal meal_style=breakfast have=chicken=2kg,rice=5kg min_protein_g=40"
)
```

The grammar supports `meal`/`meals`, `workflow`, and `scrape source=ID[,ID]`. It accepts only known
`key=value` fields, with a 4096-character/64-token bound. It never executes a shell or evaluates
expressions. Keys, source IDs and pantry quantities are validated; repeated keys or stock entries
are errors. Money and nutrient values are decimal strings. Explicit null policy overrides are
invalid; omit a key to inherit it. Serialized commands omit unset policy values and round-trip.

Meal overrides include `meal_style`, `servings`, `min_protein_g`, `max_kcal`,
`max_cost_per_serving_czk`, `max_stores`, `max_age_hours`, `ranking`, `lactose_free`, `allow_loyalty`,
and `retailers`. `have`, `use_first`, and `have_seasonings` supply the shared pantry overrides.
Text commands do not select arbitrary locations, acquisition URLs or server paths.

`MealService(reader, catalog, clock).plan(command, profile=None)` resolves catalog defaults,
optional saved `MealParameters`, then request overrides. A style change applies the configured
style limits before explicit request limits; unchanged saved profiles retain their own limits.
The catalog and effective pantry are copied so requests cannot change shared defaults.

`WorkflowService` validates preferences before acquisition, then pins planning to the run it just
completed. `meals --run-id UUID` pins an existing successful run from the configured source.
Explicit selection still enforces planning freshness, offer validity and item freshness. Other
requests or failed subsequent runs cannot silently change the selected observation states.

## Independent migrations

```sh
.venv/bin/grocery-agent db upgrade offers
.venv/bin/grocery-agent db status offers

# Optional; ordinary commands never need this:
GROCERY_CONTROL_DATABASE_URL=sqlite:///data/control.db \
  .venv/bin/grocery-agent db upgrade control
.venv/bin/grocery-agent db status control
```

| Database | Revision | Tables |
| --- | --- | --- |
| Offers, default `data/grocery.db` | `offers_0002` | Seven original history/evidence tables, catalogue heads/entries, `offers_schema_version` |
| Optional control, default `data/control.db` | `control_0001` | `users`, `user_profiles`, `control_schema_version` |

Opening an engine does not create schemas. Readers use read-only connections and never migrate.
Offer writers automatically upgrade offer storage while holding the CLI/collector writer lock.
Control migrations run only through an explicit control operation; mixed databases are refused.
The control schema is the preparation for phase 2, without account provisioning or authentication.

An unversioned original offer database is adopted only if all seven tables, columns, types,
nullability, keys and indexes match the frozen baseline. Adoption stamps the baseline and creates
the derived cache tables; historical rows and raw references remain unchanged. Partial or unknown
schemas are refused before stamping. Read commands can still inspect an intact original baseline
without writing a version table, but catalogue commands need the new cache tables.

Back up database files, snapshots and reports with all processes stopped before upgrades. There
is no CLI downgrade operation; use a reviewed migration or restore a complete backup for rollback.
SQLite adoption/history preservation is covered offline. PostgreSQL uses portable SQLAlchemy
types and queries, but has not been exercised against a PostgreSQL server.

## Request artifacts and concurrency

Successful requests publish `reports/requests/<request UUID>/report.html`, `report.json`, and
`request.json` via a directory rename. The manifest retains resolved parameters, pantry, selected
run, recipe catalog snapshot and catalog hash. Reusing a request ID refuses to overwrite its result.
There is no server path supplied by a command.

The CLI normally updates `latest.html`/`latest.json` as local convenience files. `--no-latest`
keeps only the isolated request artifacts and allows meal reads while the collector holds the
acquisition lock. Future server handlers must use isolated artifacts with account ownership from
control storage; a UUID directory is not authorization. The local `latest` aliases are not private
multi-user publication. No profile is opened or saved by ordinary commands.

See [catalogue operations](catalogue.md) for complete publication, failed refreshes, expiration,
scheduling and Docker process roles, and [meal styles](meals.md#meal-styles) for the recipe options.
