# Usage and requirements

## What you need

| Requirement | Purpose |
| --- | --- |
| Linux and Python 3.13+ with `venv` and pip | Run the CLI; the process lock currently uses Linux/Unix `fcntl` |
| A checkout of this repository | Supplies the source, fixture data and `config/meals.toml` |
| Installed Python dependencies | HTTP, HTML parsing, validation and SQLite persistence |
| Internet access to `https://www.kupi.cz` | Collect fresh offers; generating meals from fresh saved data works offline |
| Write access to the project data directory | Store the database, raw snapshots, lock file and reports |
| A browser | Read the local HTML recipe report |

Kupi uses public HTML. The current workflow needs no retailer account, cookies, API key, local
LLM, OpenAI subscription, browser automation, separate database server, or background service.
SQLite is created automatically. Raw snapshots and history accumulate, so disk usage depends on
how many categories and runs you retain; there is no automatic retention cleanup yet.

Runtime dependencies are declared in `pyproject.toml`: httpx, BeautifulSoup, lxml, Pydantic,
pydantic-settings, SQLAlchemy, Alembic and timezone data. Pip installs them. pytest, Ruff and mypy
are development tools. FastAPI/Uvicorn are optional for the private catalogue API.
Playwright and PostgreSQL support are optional extras for future requirements.

## Install once

From the checkout on this machine:

```sh
cd ~/Documents/grocery-agent
python3 --version
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/grocery-agent --help
```

The interpreter used to create `.venv` must be Python 3.13 or newer. If your default `python3`
is older, use an installed `python3.13` or `python3.14` instead. If a virtual environment is
already installed, use it; the following commands do not require shell activation.

Configuration has working defaults. Optionally copy `.env.example` to `.env` once and edit it.
Preserve an existing `.env`. Set `GROCERY_USER_AGENT` to a descriptive project identity/contact
if you customize the acquisition settings. `.env` and runtime data are ignored by Git.

Run all examples from the **repository root**. Relative configuration and data paths use the
working directory, not the location of the installed executable.

## Recommended one-off meal run

This collects meat, vegetables and cooking staples, then creates a meal report:

```sh
cd ~/Documents/grocery-agent
GROCERY_KUPI_CATEGORY_SLUGS='["maso-drubez-a-ryby","ovoce-a-zelenina","vareni-a-peceni"]' \
  .venv/bin/grocery-agent workflow
```

The category override applies to this command only. If you previously set
`GROCERY_KUPI_LISTING_URL`, remove/comment it in `.env` and unset it in your shell first: a
single listing URL takes precedence over the category list. Other environment settings still
take precedence over `.env`.

The process runs in the foreground, saves its outputs, then exits. It installs no timer or
service. The verified three-category run on **2026-10-01** took about **3 minutes 40 seconds**
and accepted **1,489 offers**, with zero rejected records/errors and 269 changed states.
Duration and offer counts vary with the source.

Open `data/reports/latest.html` in your browser, or use your Linux desktop opener:

```sh
xdg-open data/reports/latest.html
```

The report contains ranked dishes, preparation steps, protein/calories/carbs/fat per serving,
ingredient quantities, retailer links, offer validity and conditions, and estimated ingredient
usage costs. For details on how costs and nutrition are calculated, see [meal planning](meals.md).

### Example from the verified run

The October 1 test selected **smoky chicken with red lentils and roasted carrots**: 300 g raw
chicken breast, 80 g dry lentils, 200 g carrots, 50 g onion and 10 g rapeseed oil for one large
serving. Keep salt, pepper, cumin and smoked paprika available for seasoning.

Its estimated macros were **90 g protein, 708 kcal, 42 g available carbohydrates and 15 g fat**.
The two-chain basket used Billa and Albert and cost approximately **53.92 Kč in ingredients used**,
including estimated oil and seasoning costs. These are dated example results; a later run can
select different offers, stores or recipes. Whole-pack checkout costs may be higher.

## Other commands

Meal reports use one configured source; the default remains Kupi.

| Command (after `.venv/bin/grocery-agent`) | Behavior |
| --- | --- |
| `stores` | List acquisition sources: `kupi`, `mock` |
| `workflow` | Acquire the meal profile's source, validate/persist, then generate meals |
| `scrape kupi` | Acquire and persist Kupi offers; does not generate a meal report |
| `meals` | Generate meals from the latest complete, fresh saved Kupi batch; no network requests |
| `meals --meal-style breakfast` / `meals --meal-style snack` | Select an optional meal style; main remains the default |
| `meals --no-latest` | Save an isolated request without updating local `latest` aliases |
| `command 'meal meal_style=snack have=chicken=2kg'` | Run the shared structured-command parser |
| `catalogue kupi --unit kg --sort unit_price` | Query the persisted, expiry-aware catalogue |
| `collector --once` | Collect configured sources and publish each complete batch once |
| `collector` | Start the configurable daily collection process; does not install an OS timer |
| `serve-catalogue` | Optional private read-only HTTP app; requires the API extra |
| `db upgrade offers` / `db status offers` | Explicit offer migrations / read-only revision inspection |
| `db upgrade control` | Initialize the optional separate user/profile schema; ordinary commands do not need it |
| `runs --limit 5` | Show recent acquisition status, timestamps, counts and errors as JSON |
| `scrape mock` | Exercise fixture acquisition and persistence offline; does not provide real prices |
| `scrape --all` | Acquire all registered sources, including the mock; does not generate meals |

The aggregator source is `kupi`, even when an offer is sold by Billa, Albert or Tesco.
With no category or listing override, `workflow` and `scrape kupi` cover **13 categories**.
The verified full run took about **19 minutes**. Use:

```sh
.venv/bin/grocery-agent workflow
```

To use an existing fresh collection immediately:

```sh
.venv/bin/grocery-agent meals
```

Planning uses only offers actually observed in the latest successful batch for the configured
source. A focused scrape therefore supplies a focused batch, even if the database retains older
offers from all 13 categories. After a failed refresh, a published prior complete batch can be
used within the configured freshness limit, with warnings; expired offers stay hidden. Use
`--strict-latest` to require the latest attempt itself to succeed. Legacy data without a published
head retains that strict behavior. Rebuild a complete saved run or rescan to populate the cache.
The offline mock is independent of the real Kupi batch. See [cache and Docker usage](catalogue.md).

## Configure acquisition and file locations

Use environment variables for temporary overrides or `.env` for persistent ones:

| Setting | Default / meaning |
| --- | --- |
| `GROCERY_DATABASE_URL` | `sqlite:///data/grocery.db` |
| `GROCERY_SNAPSHOT_DIR` | `data/snapshots` |
| `GROCERY_MEAL_CONFIG` | `config/meals.toml` |
| `GROCERY_REPORT_DIR` | `data/reports` |
| `GROCERY_LOCK_PATH` | `data/workflow.lock`; all CLI writers must use the same lock |
| `GROCERY_COLLECTOR_CONFIG` | `config/collector.toml`; sources, local time and timezone |
| `GROCERY_CATALOGUE_MAX_AGE_HOURS` | `36`; cache freshness ceiling, independently of meal policy |
| `GROCERY_CATALOGUE_ALLOW_CACHED_ON_FAILURE` | `true`; visible fallback after unsuccessful refresh |
| `GROCERY_CONTROL_DATABASE_URL` | Optional `sqlite:///data/control.db`; explicit control operations only |
| `GROCERY_HTTP_TIMEOUT_SECONDS` | `30` per HTTP operation |
| `GROCERY_LOG_LEVEL` | `INFO`; also accepts DEBUG, WARNING, ERROR, CRITICAL |
| `GROCERY_KUPI_CATEGORY_SLUGS` | JSON list of category slugs; default is all 13 |
| `GROCERY_KUPI_LISTING_URL` | Optional single unfiltered category URL, overriding the list |
| `GROCERY_KUPI_MAX_PAGES` | `100` **per category**; reaching the limit with pages remaining fails the run |
| `GROCERY_KUPI_REQUEST_DELAY_SECONDS` | `1`; source crawl rules may require longer spacing |
| `GROCERY_KUPI_ATTEMPTS` | `3` attempts for retryable failures |

For a persistent meal-focused selection, add this line to `.env`:

```dotenv
GROCERY_KUPI_CATEGORY_SLUGS=["maso-drubez-a-ryby","ovoce-a-zelenina","vareni-a-peceni"]
```

Category values must be a nonempty JSON list of unique slugs. Do not use comma-separated text.
See [Kupi acquisition](kupi.md) for supported categories, pagination, source mapping and limitations.

## Configure meals

Edit the existing `[policy]` section in `config/meals.toml`:

| Field | Current profile / meaning |
| --- | --- |
| `lactose_free` | `true`; only recipes with lactose-free ingredient entries qualify |
| `meal_style` | `"main"`; CLI/text also select `breakfast` and `snack` |
| `servings` | `1`; scales shopping quantities, while displayed macros/cost remain per serving |
| `min_protein_g` | `"70"` per serving |
| `max_kcal` | `"850"` per serving |
| `max_cost_per_serving_czk` | `"100"` in ingredients used |
| `max_stores` | `2`; maximum retailer chains per basket, supported range 1–3 |
| `retailers` | Seven ordinary supermarket chains; an empty list considers all sellers |
| `allow_loyalty` | `false`; set true only if you can meet the displayed membership terms |
| `ranking` | `"protein_per_czk"`; use `"protein"` to rank by absolute protein |
| `max_age_hours` | `36`; both batch and individual observations must be fresh |
| `seasoning_allowance_czk` | `"3"` per serving, an editable pantry cost estimate |

Keep Decimal values such as protein, calories and prices **quoted** in TOML. Decimal strings
avoid binary floating point. Whole numbers can also be integers; fractional values must be
quoted strings rather than TOML floating-point literals.

Acquisition currently records Kupi's displayed locality, observed as **Praha**. The meal profile
uses `scope = "kupi:locality:praha"`, `location_label = "Praha"` and `timezone = "Europe/Prague"`.
Changing the label or scope does not make the adapter acquire a different city; explicit locality
selection is not implemented yet.

The catalog currently has eight ingredient entries and eight fixed recipes: four main dishes,
two breakfasts and two snacks. Main dishes remain the default. See [meal styles](meals.md#meal-styles)
for defaults and selection. Changing a protein
limit filters these recipes; it does not automatically increase portion sizes or invent recipes.
Edit `grams` inside a recipe to change its amounts **per serving**, or add a recipe using known
ingredient IDs. Meat is raw, lentils/rice are dry, and vegetables are weighed before trimming.
Ingredient entries supply matching rules, edible yields and attributed nutrition values.
Oil is a pantry estimate; its configured price is `"200"` Kč/kg. See [meal planning](meals.md)
for adding ingredients and interpreting the estimates.

### Food you already have

Supply pantry stock when generating a report. For example, with 5 kg dry rice and 2 kg raw
chicken breast already at home:

```sh
.venv/bin/grocery-agent meals --have rice=5kg --have chicken=2kg \
  --have oil=available --have-seasonings --use-first chicken
```

The same flags work with `workflow`. Repeat `--have` for each ingredient. Supported ingredient
IDs are `chicken`, `turkey`, `lentils`, `rice`, `carrot`, `onion`, `oil` and `oats`. Use positive `g` or
`kg` quantities, including decimals such as `chicken=0.25kg`. `oil=available` means you have
enough for any suggested dish; a measured amount such as `oil=50g` tracks shortages instead.
`--have-seasonings` marks the salt, pepper, cumin, paprika and other recipe seasonings as already
available, removing their allowance from the price. Individual spices are not quantified.

Owned quantities cost **0 Kč**. For a dish needing 600 g chicken across two servings, 400 g
in stock means buying and pricing only 200 g. Nutrition still includes all 600 g. Fully covered
ingredients need no offer or store visit. A shortage still needs an eligible offer (or the existing
oil estimate). The chicken-and-rice dish can use both of the example's owned ingredients, with
only vegetables left to buy when oil and seasonings are also available.

`--use-first chicken` puts dishes using that owned ingredient ahead of the normal protein/cost
ranking. This lets you favor food you want to use soon. Repeat the option to mark other stock;
it requires stock supplied by `--have` or configuration. Amount used from marked stock decides
priority, then the configured ranking breaks ties. Expiry dates are not tracked automatically.

For persistent stock, uncomment the pantry example in `config/meals.toml` or add:

```toml
[pantry]
seasonings_available = true

[pantry.items.rice]
grams = "5000"

[pantry.items.chicken]
grams = "2000"
use_first = true

[pantry.items.oil]
# No grams means enough is already available for any suggestion.
```

The report shows **Use**, **Already have** and **Buy** masses across all servings. Each dish is
an alternative evaluated against the same inventory; dishes do not share an allocated stock
budget. Nothing is deducted after generating a report. Update quantities after cooking.
CLI quantities temporarily replace the configured stock for that ingredient; repeated entries
for the same ingredient are rejected to avoid ambiguous requests. They do not edit the TOML file. Reports retain
the effective inventory in JSON. No owned stock is assumed by default, and acquisition freshness
rules still apply, including for fully stocked dishes.

## Outputs and history

| Path | Contents |
| --- | --- |
| `data/reports/latest.html` | Latest local recipe report, or a pending/failure notice |
| `data/reports/latest.json` | Latest machine-readable report, or failure status |
| `data/reports/<timestamp>.json` | Archived successful meal reports with their inputs and run ID |
| `data/reports/requests/<UUID>/` | Isolated HTML/JSON report and request manifest with resolved parameters, catalog snapshot/hash and run ID |
| `data/grocery.db` | Offer history, acquisition runs, item provenance and indexed catalogue |
| `data/snapshots/` | Content-addressed raw source bytes referenced by the database |
| `data/workflow.lock` | Advisory lock file; its presence alone does not mean a job is running |

`--no-latest` writes only the per-request directory and can read the completed catalogue while
a collector is running. `meals --run-id UUID` selects an explicit successful batch from the
configured source, still subject to the meal policy's freshness and validity checks. Accounts
are unnecessary for every acquisition, catalogue and meal command. The optional control database
is created only by explicit control migrations; login and private multi-user APIs remain planned.

For the separate collector, HTTP API, Docker paths, scheduling and backups, see
[catalogue operations](catalogue.md). For native migration/adoption and the command contract,
see [phase 1 foundation](phase-one.md).

Identical consecutive offer states update their last-seen time without creating another price
history state. Every received observation still retains provenance. Back up the SQLite database
and snapshots together while the writer is stopped; back up reports as well if you want the
recommendation archive. Generated reports and stored offers are date-specific; an old quoted
price is not a current shopping recommendation.

## Logs and troubleshooting

JSON logs go to stderr. Successful `workflow` output contains two JSON lines on stdout: the
acquisition summary and the meal report. `scrape` prints acquisition summaries; `meals` prints
one report; `runs` prints a JSON array. To save a full run's console output:

```sh
mkdir -p data/logs
.venv/bin/grocery-agent workflow > data/logs/workflow.jsonl 2> data/logs/workflow.log
```

These log files are replaced each time that example is run. Exit code 0 means success, 1 means
an operational/validation/planning failure, 2 is an argument error, and 130 means keyboard
interruption. An acquisition failure preserves accepted history but does not produce a new meal.

| Symptom | What to check |
| --- | --- |
| Missing meal config or unexpected empty database | Run from the repository root; check configured paths |
| Missing, stale or unsuccessful latest batch | Inspect `runs --limit 5`; run a fresh successful `workflow` |
| No complete recipe meets the limits | Required ingredients may have no eligible offers; review category selection, retailer limit, loyalty and protein/calorie/cost limits |
| Another workflow holds the writer lock | Let the active process finish; a leftover lock file is harmless after process exit |
| Page limit or incomplete category coverage | Review logs and `GROCERY_KUPI_MAX_PAGES`; partial results are not a complete batch |
| HTTP 403/429 or changed page markup | Review source errors and saved evidence; wait for source access/rate recovery or update the adapter |
| Report's out-of-date banner | Regenerate with `meals` if data is fresh, otherwise use `workflow`; reload the report |
| Run remains `running` after forced termination | Accepted items survived; after the process exits, complete a new run; automatic stale-run repair is not implemented |

Loyalty, minimum-purchase and outlet terms still matter. The headline cost covers the amount used,
including estimated oil/spices; whole packs may cost more. Nutrition is based on generic raw-food
data and trimming yields, rather than the exact product's label. Source stock is generally unknown.

## Development setup

See [development](../development.md) for the pinned environment, checks, CI and extension guides.
