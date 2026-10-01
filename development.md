# Development

## Purpose and scope

Grocery Agent collects Czech grocery offers and keeps auditable price history. Acquisition
adapters emit one canonical schema; validation, persistence, matching and meal planning operate
without retailer-specific branches.

The current project includes an offline MockStore and Kupi acquisition across 13 grocery
categories and multiple retailers. These are advertised promotions with locality, validity and
loyalty conditions, rather than complete retailer inventories. The meal planner uses curated
ingredient nutrition and three fixed recipes. Direct retailer adapters, fuzzy matching, general
shopping optimization, agents and scheduling remain future work.

## Development environment

Create a Python 3.13+ virtual environment, then install the pinned tooling and editable project
from the repository root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation -e .
```

The runtime dependencies include HTTP acquisition, HTML parsing, Pydantic validation and
SQLAlchemy persistence. Browser automation (`.[browser]`) and PostgreSQL (`.[postgres]`) are
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
2026-10-01 passed 234 tests plus Ruff and mypy; that count is a dated result.

## Architecture and contracts

- [Architecture and database schema](docs/architecture.md)
- [Acquisition adapter contract](docs/adapter-contract.md)
- [Kupi parsing, source mapping and verification](docs/kupi.md)
- [Meal planning, pricing and nutrition assumptions](docs/meals.md)
- [Runtime configuration and troubleshooting](docs/usage.md)

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

CLI writers share an advisory process lock. Embedded pipeline users must coordinate the same
lock. SQLite transactions are short and synchronous; use one writer process. Schema creation
bootstraps the database; introduce migrations before changing a deployed SQL schema.

The database is created automatically. Raw snapshots are content-addressed files under
`data/snapshots`; back up the database and snapshots together while the writer is stopped.
JSON logs go to stderr and CLI summaries to stdout. Failed, partial and empty runs exit nonzero.
Run counters and retained source evidence support debugging and future coverage monitoring.

Abrupt termination can leave a run marked `running`; accepted item transactions survive.
Cancellation handled by the process marks the run `cancelled`. Automatic stale-run recovery,
snapshot retention and scheduling remain deferred. See [usage](docs/usage.md) for recovery steps.

## GitHub setup

For a repository that has no remote configured, create the GitHub repository and then run:

```sh
git remote add origin git@github.com:YOUR_USER/grocery-agent.git
git push -u origin main
```

For an existing checkout, inspect `git remote -v` before configuring a remote. Use meaningful
conventional commits for distinct implementation phases. Runtime data and `.env` belong outside
commits; saved test fixtures should contain only the source information needed by parser tests.
