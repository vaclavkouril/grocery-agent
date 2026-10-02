# Development

## Purpose and scope

Grocery Agent collects Czech grocery offers and keeps auditable price history. Acquisition
adapters emit one canonical schema; validation, persistence, matching and meal planning operate
without retailer-specific branches.

The current project includes an offline MockStore, Kupi acquisition across 13 grocery categories
and multiple retailers, and a direct Rohlík adapter covering eleven public online food/drink
categories. Kupi supplies advertised promotions with locality, validity and loyalty conditions;
Rohlík also supplies ordinary online catalog prices, pack quantities and stock. The meal planner
uses curated ingredient nutrition and eight fixed recipes: four main dishes, two breakfasts and
two snacks. Main dishes remain the default. Account-independent application services, independent
migrations, an expiry-aware persisted catalogue, optional read API and scheduled collector are
implemented. Additional acquisition coverage, fuzzy matching, general optimization and agents
remain future work.

The direct Tesco adapter reads ordinary online department listings with an isolated Chromium
browser and the existing optional browser extra. It handles standard and Clubcard variants,
mass quotes, pack quantities and coverage checks. Live pagination is currently blocked by Tesco
access denial; see [Tesco acquisition and verification limits](docs/tesco.md).

The Makro adapter uses a saved customer login and selected branch, with explicit VAT-inclusive
purchase terms. Authenticated price markup and complete live acquisition remain unverified;
see [Makro setup and limits](docs/makro.md).

## Development environment

Create a Python 3.13+ virtual environment, then install the pinned tooling and editable project
from the repository root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation -e .
```

The runtime dependencies include HTTP acquisition, HTML parsing, Pydantic validation, Alembic
migrations, timezone data and SQLAlchemy persistence. The private catalogue API (`.[catalogue-api]`),
browser automation (`.[browser]`) and PostgreSQL (`.[postgres]`) are
optional extras; install them when an adapter or deployment requires them.

## Checks

```sh
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy
.venv/bin/pytest
```

Normal tests use saved fixtures and temporary databases and forbid live network access. Optional
network tests belong under the `live` marker. GitHub Actions runs linting, formatting, type
checking and offline tests on Python 3.13 and 3.14. The implementation verification on
2026-10-01 passed 903 offline tests, one fixture-backed desktop/mobile combined-source browser test,
frontend Node contracts, Ruff lint/format checks and mypy (122 source files). A wheel build and
packaged migration/entrypoint checks passed. Docker's Compose plugin is unavailable locally,
so image/Compose execution remains unverified; CI includes fixture-backed acquisition smoke checks.

## Architecture and contracts

- [Compact project context and handoff](docs/context.md)
- [Architecture and database schema](docs/architecture.md)
- [Acquisition adapter contract](docs/adapter-contract.md)
- [Kupi parsing, source mapping and verification](docs/kupi.md)
- [Rohlík online catalog, source mapping and configuration](docs/rohlik.md)
- [Tesco online catalog, browser setup and access limits](docs/tesco.md)
- [Makro login, branch scope and minimum purchase costs](docs/makro.md)
- [Meal planning, pricing and nutrition assumptions](docs/meals.md)
- [Runtime configuration and troubleshooting](docs/usage.md)
- [Proposed server UI, multi-user controls, email and SimpleX plan](docs/server-access-plan.md)
- [Implemented phase 1 services, commands and separate databases](docs/phase-one.md)
- [Collector, catalogue API, cache policy and Docker](docs/catalogue.md)
- [Profile-aware acquisition, migration and cached profile selection](docs/acquisition-profiles.md)
- [Shared local-first recipes, cache policies and combined sources](docs/recipe-service.md)

Acquisition is streamed through `AcquisitionAdapter`; single-retailer adapters use its
`StoreAdapter` specialization. Adapters supply canonical candidates and source evidence and
keep SQL, product identity resolution and nutrition calculations outside their implementations.
The validation pipeline persists through `OfferRepository`. Meal planning reads complete batches
through `CurrentOfferReader` and treats retailer IDs as opaque identities.

## Adding a retailer

1. Add `src/grocery_agent/stores/<retailer>/` with an adapter and a pure fixture parser.
2. Implement `StoreAdapter.fetch_offers(context)` as an async iterator of `AcquisitionItem`.
3. Emit canonical candidate fields and source evidence; retain source-only fields in the evidence.
4. Register the adapter factory in `stores/registry.py`.
5. Add saved fixtures, parser tests and a case in `tests/test_adapter_contract.py`.

For an aggregator representing multiple retailers, implement `AcquisitionAdapter` with a
`source_id` and `validate_offer(offer)` identity check. Candidates carry the actual seller in
`product.store_id`. `StoreAdapter` supplies the single-retailer identity check automatically.

Persistence and downstream services need no changes when a new adapter satisfies the contract.
Read the detailed adapter contract for price basis, promotion variants, provenance, units and
item-level failures before implementing one.

## Extending meals

Recipes, ingredient matching, generic nutrition values and limits live in `config/meals.toml`.
Keep ingredient matching separate from exact cross-store product identity. New ingredients need
attributed nutrition values and conservative matching rules; new recipes need quantities and
preparation steps. Cover changes with offline tests, including price eligibility, lactose
requirements, freshness, units, serving arithmetic and retailer-count limits.

See [meal planning](docs/meals.md) for how raw/dry quantities, edible yields, pantry estimates
and ingredient usage costs differ from a checkout total.

## Persistence and operations

Settings use `GROCERY_` environment variables or `.env`, with environment variables taking
precedence. Paths resolve from the process working directory. Runtime data, environments and
caches are ignored by Git.

CLI acquisitions, collector rounds and migrations share an advisory process lock. Embedded
pipeline users must coordinate the same lock. SQLite uses WAL, explicit transactions and a busy
timeout. Use one acquisition writer process. Meal services read pinned history/cache independently;
`meals --no-latest` saves isolated reports while collection is running. Readers never migrate.
Offer writers upgrade through the offers Alembic history; control migrations remain explicit.

The database is created automatically. Raw snapshots are content-addressed files under
`data/snapshots`; back up the database and snapshots together while the writer is stopped.
JSON logs go to stderr and CLI summaries to stdout. Failed, partial and empty runs exit nonzero.
Run counters and retained source evidence support debugging and future coverage monitoring.

Abrupt termination can leave a run marked `running`; accepted item transactions survive.
Cancellation handled by the process marks the run `cancelled`. Automatic stale-run recovery and
snapshot retention remain deferred. Scheduling is available through the independent collector,
but no process or OS timer was activated during implementation. See [usage](docs/usage.md) and
[catalogue operations](docs/catalogue.md) for recovery and manual deployment steps.

## GitHub setup

For a repository that has no remote configured, create the GitHub repository and then run:

```sh
git remote add origin git@github.com:YOUR_USER/grocery-agent.git
git push -u origin main
```

For an existing checkout, inspect `git remote -v` before configuring a remote. Use meaningful
conventional commits for distinct implementation phases. Runtime data and `.env` belong outside
commits; saved test fixtures should contain only the source information needed by parser tests.
