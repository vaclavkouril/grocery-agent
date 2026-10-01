# Grocery Agent

A modular foundation for collecting Czech grocery offers and keeping auditable price history.
Retailer adapters emit one canonical schema; validation, persistence, matching, and future analytics
operate without retailer-specific branches. Includes an offline MockStore and a Kupi adapter
collecting advertised offers from multiple retailers.

Start with [usage and requirements](docs/usage.md) for installation, one-off runs, configuration,
reports and troubleshooting. See [architecture](docs/architecture.md) and the
[adapter contract](docs/adapter-contract.md) for implementation details.

## Setup

Python 3.13+ is required. From this repository:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.lock
python -m pip install --no-deps --no-build-isolation -e .
grocery-agent stores
GROCERY_KUPI_CATEGORY_SLUGS='["maso-drubez-a-ryby","ovoce-a-zelenina","vareni-a-peceni"]' \
  grocery-agent workflow
xdg-open data/reports/latest.html
```

Settings work without `.env`. To persist overrides, copy `.env.example` to `.env` once, preserving
any existing configuration. An existing `GROCERY_KUPI_LISTING_URL` override takes precedence over
the category list; remove it before using the meal-focused selection above.

HTML parsing is included for Kupi. Browser automation (`.[browser]`) and PostgreSQL (`.[postgres]`)
are optional extras. Install them when an adapter or deployment needs them.
The committed development lock pins the tested tooling and dependencies. For a runtime-only
installation, use `python -m pip install .` instead.

## Development

```sh
ruff check .
ruff format --check .
mypy
pytest
```

Tests use saved fixtures and temporary databases; normal tests never need live retailers.
CI runs the same checks on Python 3.13 and 3.14.
Run commands from the repository root. Runtime data, environments and caches are ignored by Git.

## Scope

This is the acquisition foundation with one real acquisition source. Kupi scrapes 13 grocery
categories across retailers: produce, meat/fish, dairy/eggs, bakery, canned food, deli, frozen/instant
food, drinks, alcohol, snacks, cooking/baking, health food and baby food/care. It retains displayed
locality, loyalty conditions and validity dates. It does not collect full retailer inventories or
establish exact cross-store identity.
See [Kupi mapping, configuration and verification](docs/kupi.md).

Direct retailer adapters, fuzzy matching, general shopping optimization, agents, and scheduling
remain future work. `--all` runs both registered sources, including the offline mock.

## One-off meal suggestions

`grocery-agent meals` generates a local HTML/JSON report from the latest complete, fresh offers.
`grocery-agent workflow` scrapes the configured source and then generates the report in one run.
No timer is activated. Open `data/reports/latest.html`; see [meal workflow](docs/meals.md).
The quick-start category selection above took about four minutes in the verified run.
With no category/listing override, `workflow` collects all 13 categories, taking about 19 minutes
in the verified full run. `grocery-agent meals` reuses fresh saved data without another scrape.

The initial profile is Praha, lactose-free, one large serving, at least 70 g protein, and up to two
supermarket chains. Edit recipes, nutrition sources and limits in `config/meals.toml`. Cost estimates
cover the amount used; whole packs, travel and stock are not guaranteed. Oil/spices use explicit
pantry estimates. Meal planning consumes canonical data through a read interface without retailer
branches or LLM dependencies.

## Adding a retailer

1. Add `src/grocery_agent/stores/<retailer>/` with an adapter and pure fixture parser.
2. Implement `StoreAdapter.fetch_offers(context)` as an async iterator of `AcquisitionItem`.
3. Emit canonical candidate fields and source evidence; keep raw retailer fields in the evidence.
4. Register the factory in `stores/registry.py`.
5. Add saved fixtures, parser tests, and a case in `tests/test_adapter_contract.py`.

For an aggregator representing several retailers, implement `AcquisitionAdapter` with a `source_id`
and `validate_offer(offer)` identity check. Candidates still carry the actual retailer in
`product.store_id`. Single-retailer `StoreAdapter` supplies this check automatically.

Persistence and downstream services need no changes. See the detailed contract before implementing
an adapter, especially price basis, promotion variants, provenance, and item-level failures.

## Configuration and operations

Settings use `GROCERY_` environment variables or `.env`; environment variables take precedence.
Paths resolve from the process working directory. Use absolute paths and the virtual environment
executable when configuring a future systemd timer. CLI writers share an advisory process lock.

The database is created automatically. Raw snapshots are content-addressed under `data/snapshots`.
Back up the database and snapshot directory together. JSON logs go to stderr; CLI summaries go to
stdout. Scrapes with rejected records, operational errors, or zero offers exit nonzero. `runs` shows
persisted counts and start/end times. Zero results are flagged for review, without declaring products
unavailable or deleting previous history.
Abrupt process termination can leave a run marked `running`; its already committed items survive.
Cancellation handled by the process marks the run `cancelled`. Automatic stale-run recovery and
snapshot retention are deferred until deployment requirements are known.

## GitHub and license

This repository is initialized locally. No remote is assumed. After creating a repository on GitHub:

```sh
git remote add origin git@github.com:YOUR_USER/grocery-agent.git
git push -u origin main
```

MIT is the suggested license: it is simple and permissive. No license grant has been selected yet;
see [license decision](docs/license.md) before publishing as open source.
