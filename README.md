# Grocery Agent

Runtime source is under `src/`, including the independent website in `src/frontend/`.
Installed applications can run outside the checkout with explicit runtime paths and
[shared/application TOML configuration](docs/configuration.md).
Live-check prerequisites and deferred account setup are recorded in [acceptance](docs/acceptance.md).

Collect Czech grocery offers from Kupi and direct retailer catalogs, keep price history, and create local
protein-focused meal reports.

## Requirements

- Linux and Python 3.13+ with pip and `venv`.
- Internet access for scraping and writable space for the database, snapshots and reports.
- A browser to open the HTML report.

Dependencies are installed below. SQLite is created automatically; no API keys are needed.
Docker deployments also need Docker Engine and Compose v2.

Optional API (invite-only by default, with configurable self-registration) and durable recipe worker:
see [backend quickstart](docs/backend.md).
This includes authenticated jobs, private reports, pinned grocery inputs, administrator refresh
and independently configured API/worker processes. The backend also serves a separate static
[website](docs/website.md) with pantry controls, offers and private reports; CLI delegation and
typed Python clients use the same API. Optional [email and SimpleX clients](docs/channels.md)
support verified bindings and confirmed commands; both remain disabled by default.

The website supports Czech and English, separate recipe-language preferences, explicitly saved
pantry stock and reusable presets. Upgrade the control database with
`.venv/bin/grocery-agent db upgrade control` before starting the updated API/worker;
see [account state and revision-safe updates](docs/backend.md#account-preferences-pantry-and-presets).

## Install

Run from the repository root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

Use a Python 3.13+ interpreter. Settings work without `.env`; copy `.env.example` to `.env`
if you want persistent overrides, preserving any existing file.

## Run once

Collect meat, vegetables and cooking staples, then generate recipes (about four minutes in
the verified run):

```sh
GROCERY_KUPI_CATEGORY_SLUGS='["maso-drubez-a-ryby","ovoce-a-zelenina","vareni-a-peceni"]' \
  .venv/bin/grocery-agent workflow
xdg-open data/reports/latest.html
```

Remove any `GROCERY_KUPI_LISTING_URL` override before using this category selection.

For all 13 categories, run `.venv/bin/grocery-agent workflow` without category/listing overrides
(about 19 minutes in the verified run). To regenerate from fresh saved offers without scraping:

```sh
.venv/bin/grocery-agent meals
```

The default meal profile is Praha, lactose-free, one serving. Edit [config/meals.toml](config/meals.toml)
to change preferences. Reports are written to `data/reports/latest.html` and `latest.json`.
Meal and scrape commands run once; no timer is installed.

Use food you already own at zero purchase cost, and buy only the missing quantities:

```sh
.venv/bin/grocery-agent meals --have rice=5kg --have chicken=2kg \
  --have oil=available --have-seasonings --use-first chicken
```

These options also work with `workflow`. The report separates quantities to use, already owned,
and to buy. Save recurring pantry choices in `config/meals.toml`; see [pantry setup](docs/usage.md#food-you-already-have).

Main dishes are the default. Select a smaller meal style and optionally change its limits:

```sh
.venv/bin/grocery-agent meals --meal-style breakfast --have oats=1kg
.venv/bin/grocery-agent meals --meal-style snack --min-protein 30 --budget 60
.venv/bin/grocery-agent command 'meal meal_style=snack have=chicken=2kg min_protein_g=30'
```

Each successful request also saves an isolated report under `data/reports/requests/`.
Add `--no-latest` to leave the local `latest` shortcuts alone. No user accounts are required.

## Profile-aware acquisition

`grocery-recipes` now plans locally from cached groceries, refreshing missing/stale profiles by
default. Use `--cache-only` to prevent acquisition or `--no-cache` to require a successful refresh.
CLI and backend share one recipe service; explicitly compatible sources can be combined with
preserved provenance and distinct shopping-context limits. See [recipe configuration and library
usage](docs/recipe-service.md).
Measured local-model recipe quality, speed, memory, and reproduction commands are in the
[local model benchmark report](docs/local-model-benchmark.md).
The [second benchmark round](docs/model-benchmark-round-two.md) adds five small local models,
budget Codex models, measured token usage, and weekly usage estimates.

`grocery-scrape --config config/scrape.toml` collects configured profiles once;
`grocery-collect --config config/collect.toml --once` uses the independently configured collector.
Without `--once`, collection defaults to 02:00 Europe/Prague. Profiles publish separate cache
heads; existing `grocery-agent` commands retain their legacy catalogue behavior.
See [profiles, migration and API selection](docs/acquisition-profiles.md).

## Docker collector and catalogue

Build and collect once:

```sh
docker compose build
docker compose run --rm cli collector --once
docker compose run --rm cli catalogue kupi --unit kg --sort unit_price
```

For daily collection at 02:00 Europe/Prague and a private catalogue API:

```sh
docker compose up -d collector catalogue-api
```

The API is at `http://127.0.0.1:8000/docs`. Data persists in a named volume. Edit
`config/collector.toml` or the documented environment overrides before starting it.
See [collector, cache and Docker usage](docs/catalogue.md) for expiry, configuration and backups.

See [usage and troubleshooting](docs/usage.md) or [development](development.md).

## Rohlík online prices

Collect Rohlík's public online catalog for future store comparisons:

```sh
.venv/bin/grocery-agent scrape rohlik
```

The default covers eleven food/drink categories and retains pack sizes, prices per kg/l,
stock, conditional discounts and the displayed warehouse/locality. See
[Rohlík configuration and price mapping](docs/rohlik.md) for focused category selection.
The meal workflow continues to use its configured source, Kupi by default.

## Tesco online catalog

The direct Tesco adapter traverses ordinary online departments and preserves standard prices,
pack sizes and separate Clubcard prices:

```sh
.venv/bin/grocery-agent scrape tesco
```

It requires the browser extra and Chromium. Live verification read the first produce page,
but Tesco denied page two, so a complete catalog is currently blocked on this machine. See
[Tesco setup, pickup scope and access limits](docs/tesco.md) before relying on a run's coverage.

## Makro branch assortment

Makro requires a customer login for prices. Save a local browser session, then collect the
selected branch's ordinary assortment:

```sh
.venv/bin/python -m grocery_agent.stores.makro.login
.venv/bin/grocery-agent scrape makro
```

Prices use the quoted VAT-inclusive whole package. Purchase terms expose minimum quantities,
pack increments, and known deposits/fees; unknown charges or estimated weights do not produce
a guaranteed checkout total. Live authenticated pricing still needs verification after login.
See [Makro setup and minimum purchase costs](docs/makro.md).
