# Compact project context

Updated 2026-10-11. Resume here, then read the linked subsystem documentation.
Check the dirty worktree before editing; previous waves are intentionally uncommitted.

## Intent and boundaries

Czech grocery acquisition and nutrition-aware recipes. Independent applications share a reusable
library, not application entrypoints. Local CLI needs no account. Backend is invite-only by default;
optional self-registration requires both password login and registration to be explicitly enabled.
New recipes default to Codex; existing meal commands retain fixed templates. Ingredients must have
known configured nutrition. Decimal is used for prices, quantities, macros and pantry arithmetic.
The website exposes provider/model selection directly. Backend Codex choices are restricted to
recipe-benchmarked `gpt-6.1-sol` and `gpt-6-luna`, using low reasoning; omitted model requests
pin the first configured allowed ID instead of the CLI default. All ten benchmarked local models
are enabled in the sample backend configuration, including weaker experiments. Ollama must run
on the worker host; no model downloads or service activation are automatic.
Email and SimpleX stay disabled until explicitly configured. No services, real account integrations,
production migrations or live model/retailer acceptance runs were activated by this implementation.

## Source and runtime layout

- `src/grocery_agent/`: canonical models, retailer adapters, pipeline/persistence, profile cache,
  ingredient/meal planner, providers, configuration, typed contracts, HTTP clients and commands.
- `src/grocery_agent/apps/`: scrape, collect, recipes, API/worker, email and SimpleX entrypoints.
- `src/frontend/`: static HTML/CSS/JS website and dependency-free Node contract tests.
  Wheels bundle the website and sample configuration; runtime files do not belong beside installed code.
- `config/`: editable nonsecret TOML; `tests/`: offline tests/captures; `docs/`: subsystem docs.
- `data/`: ignored offer/control SQLite DBs, evidence and local reports. One acquisition writer.
- Root Docker/Compose/CI files provide opt-in roles. Remote execution uses installed console commands
  with explicit absolute configuration/runtime paths, not a required checkout working directory.

## Implemented waves

- [Profile acquisition](acquisition-profiles.md): selected native acquisition profiles, isolated
  fingerprint heads, actual scope/coverage, atomic complete publication and failure retention.
  `grocery-scrape` runs once; `grocery-collect` runs once or schedules 02:00 Europe/Prague by default.
- [Recipe service](recipe-service.md): local-first/cache-only/refresh/no-cache, pinned combined sources,
  shopping-context limits, quote provenance, bounded model contexts, one validation repair, deterministic
  evaluation and immutable reports. Optional batches generate 1–6 alternatives from one pinned snapshot,
  with explicit partial failures. CLI/API/text/browser share request parameters and named condiments.
  Optional cuisine preferences guide model recipes and are pinned with their requests.
- [Backend](backend.md): durable owner-scoped jobs, leases/recovery, quotas/idempotency, pinned inputs,
  invitations, bearer sessions, optional password login and secure cookie/CSRF sessions.
  Administrator source/profile refreshes are coalesced and cooldown-limited without exposing other users.
- [Website](website.md): capability-driven pantry/recipe forms, offers, progress/history/private reports,
  invitation/password/token login and optional cookie resume. Shared synchronous/asynchronous HTTP client.
  Czech/English interface and independently selected recipe language; revision-safe saved pantry,
  preferences and presets, multi-store account defaults, individual recipe bookmarks and actual offer links.
  Built-in cuisine styles and quick preset saving are available directly from Recipes.
  [Recipe exports](recipe-export.md) provide Czech/English manual-entry text, ingredient paste
  lists and schema.org Recipe JSON from immutable saved meals, with private owner-scoped API access.
  Recipes never deduct stock, and UI language switches never regenerate reports.
- [Channels](channels.md), [email](email.md), [SimpleX](simplex.md): separately configured transports,
  proof-of-control binding, command confirmation, deduplicated intake and leased outbox retries.
- [Configuration](configuration.md): defaults → shared TOML → app TOML → environment → explicit options.
  Credentials are environment-only; runtime paths remain configurable independently of source location.

## Storage and compatibility

Migration heads: offers `offers_0003`, control `control_0006`. Old collections retain legacy unknown
coverage, not guessed profiles. Control migrations preserve users, sessions, jobs and channel state.
Startup requires current schemas; migrations are explicit. Back up both DBs and evidence/report files.
Old commands/catalogue endpoints/template recipes remain compatible. Library code never imports apps.

## Acquisition limits and acceptance

Kupi promotions cover multiple retailers. Rohlík public scope is anonymous locality/warehouse, not
a delivery guarantee. Tesco complete live scraping remains limited by retailer access; Makro requires
customer session/branch and live prices remain unverified. Unknown fees are not zero, estimated weights
are not guaranteed checkout quantities, and recipe costs are consumed-ingredient rather than basket totals.
Exact GTIN matching is supported; fuzzy identity and checkout ordering are outside this task.

Run offline pytest, Ruff, mypy and `npm test --prefix src/frontend`; browser fixtures require Chromium.
Current integration: 1,448 offline Python tests, 21 fixture-browser tests and 74 Node
tests passed. Ruff lint/format and mypy (137 source files) passed. The built wheel was checked for
byte-identical frontend assets, bilingual defaults and the account migration. Existing portability
tests exercise application configuration from an unrelated CWD.
Docker is installed but local Compose execution is unverified (Compose plugin unavailable).
Real Codex and local Ollama structured drafts passed after schema compatibility/ID fixes.
Live account setup and remaining Docker prerequisites are tracked in [acceptance](acceptance.md).
Mailboxes, paired contacts, Makro customer pricing and deployed reverse-proxy/TLS remain separately
configured operator acceptance. See README/development.md for commands and deployment/backups.
