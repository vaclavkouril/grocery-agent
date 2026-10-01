# Grocery Agent

Collect Czech grocery offers from Kupi and create local protein-focused meal reports.

## Requirements

- Linux and Python 3.13+ with pip and `venv`.
- Internet access for scraping and writable space for the database, snapshots and reports.
- A browser to open the HTML report.

Dependencies are installed below. SQLite is created automatically; no API keys are needed.

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
Commands run once; no timer is installed.

See [usage and troubleshooting](docs/usage.md) or [development](development.md).
