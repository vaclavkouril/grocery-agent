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
pydantic-settings and SQLAlchemy. Pip installs them. pytest, Ruff and mypy are development tools.
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

| Command (after `.venv/bin/grocery-agent`) | Behavior |
| --- | --- |
| `stores` | List acquisition sources; currently `kupi` and `mock` |
| `workflow` | Acquire the meal profile's source, validate/persist, then generate meals |
| `scrape kupi` | Acquire and persist Kupi offers; does not generate a meal report |
| `meals` | Generate meals from the latest complete, fresh saved Kupi batch; no network requests |
| `runs --limit 5` | Show recent acquisition status, timestamps, counts and errors as JSON |
| `scrape mock` | Exercise fixture acquisition and persistence offline; does not provide real prices |
| `scrape --all` | Acquire all registered sources, including the mock; does not generate meals |

The CLI source is `kupi`, even when an offer is sold by Billa, Albert or Tesco. Direct retailer
adapters such as `scrape tesco` are not implemented.

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
offers from all 13 categories. Failed or incomplete latest runs block planning. Run a new
successful acquisition to recover. The offline mock is independent of the real Kupi batch.

## Configure acquisition and file locations

Use environment variables for temporary overrides or `.env` for persistent ones:

| Setting | Default / meaning |
| --- | --- |
| `GROCERY_DATABASE_URL` | `sqlite:///data/grocery.db` |
| `GROCERY_SNAPSHOT_DIR` | `data/snapshots` |
| `GROCERY_MEAL_CONFIG` | `config/meals.toml` |
| `GROCERY_REPORT_DIR` | `data/reports` |
| `GROCERY_LOCK_PATH` | `data/workflow.lock`; all CLI writers must use the same lock |
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

The catalog currently has seven ingredient entries and three fixed recipes. Changing a protein
limit filters these recipes; it does not automatically increase portion sizes or invent recipes.
Edit `grams` inside a recipe to change its amounts **per serving**, or add a recipe using known
ingredient IDs. Meat is raw, lentils/rice are dry, and vegetables are weighed before trimming.
Ingredient entries supply matching rules, edible yields and attributed nutrition values.
Oil is a pantry estimate; its configured price is `"200"` Kč/kg. See [meal planning](meals.md)
for adding ingredients and interpreting the estimates.

## Outputs and history

| Path | Contents |
| --- | --- |
| `data/reports/latest.html` | Latest local recipe report, or a pending/failure notice |
| `data/reports/latest.json` | Latest machine-readable report, or failure status |
| `data/reports/<timestamp>.json` | Archived successful meal reports with their inputs and run ID |
| `data/grocery.db` | Offer history, acquisition runs, accepted/rejected item provenance |
| `data/snapshots/` | Content-addressed raw source bytes referenced by the database |
| `data/workflow.lock` | Advisory lock file; its presence alone does not mean a job is running |

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

For the pinned development environment, from the project root:

```sh
.venv/bin/python -m pip install -r requirements-dev.lock
.venv/bin/python -m pip install --no-deps --no-build-isolation -e .
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy
.venv/bin/pytest
```

The ordinary test suite is offline and uses fixtures and temporary databases. On 2026-10-01 the
implementation passed 234 tests plus linting and type checking; this is a dated verification,
not a fixed future test count. See the [architecture](architecture.md) and
[adapter contract](adapter-contract.md) before extending acquisition.
