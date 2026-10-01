# Compact project context

Resume from this file; use the linked documents and source files for details. Updated 2026-10-01.

## Scope and preferences

- Modular Czech grocery acquisition and protein-focused meal suggestions, primarily on Linux.
- Praha, lactose-free, high protein; main dishes default, breakfast/snack optional.
- CLI works without accounts. Multiple users are planned for server access.
- No LLM frameworks. Retailer knowledge stays in adapters; consumers use canonical offers.
- Money, quantities and macros use Decimal. Raw evidence stays outside business models.
- README contains installation/running requirements; developer detail lives in `development.md`/`docs/`.
- No service/timer was started and no commits were pushed.

## Where things live

All source paths below are under `src/grocery_agent/`.

| Path | Responsibility |
| --- | --- |
| `models/` | Product identity, store products, offers, promotions, units, purchase terms/costs |
| `stores/base.py`, `stores/registry.py` | Streaming adapter contract and factory registry |
| `stores/{kupi,rohlik,tesco,makro,mock}/` | Source config, acquisition and fixture-testable parsing |
| `stores/robots.py` | Shared robots rules; Kupi keeps a compatibility import |
| `pipeline/` | Evidence retention, canonical validation, rejected records, run counts/status |
| `persistence/` | SQLAlchemy repository, immutable price history, readers, snapshots |
| `workflow.py` | Shared process lock for acquisition and local report publication |
| `persistence/migrations/` | Independent Alembic offer/control migration trees |
| `persistence/control/` | Optional separate user/profile DB, ownership filtering, revision checks |
| `application/` | Versioned typed commands, parameter resolution, services, runtime composition, reports |
| `meals/` | Curated ingredients/recipes, pantry deductions, ranking, macros, HTML/JSON reports |
| `matching/` | Exact GTIN identity boundary; fuzzy cross-store matching deferred |
| `catalogue/` | Indexed published cache, expiry/freshness rules, read-only API |
| `collector/` | Independent scheduled collection, last-complete publication, DST-aware timing |
| `cli/main.py` | CLI composition; no dependency on accounts or a running server |

Root configuration: `config/meals.toml`, `config/collector.toml`, `.env.example`.
Deployment: `Dockerfile`, `compose.yaml`, pinned requirements, `.github/workflows/ci.yml`.
Offline tests: `tests/`, shared adapter contract, sanitized captures in `tests/fixtures/`.
Runtime: ignored `data/` contains DBs, snapshots, reports and private Makro session state.

## Implemented behavior

- Phase 1: reusable controls/services, optional separate control DB, collector/cache/API/Docker files.
- Phase 1b: styles `main` (70 g protein / 850 kcal / 100 Kč), `breakfast` (35/650/80),
  `snack` (25/450/60). Eight recipes total; explicit request limits override style defaults.
- `--have INGREDIENT=2kg|available`, `--use-first`, `--have-seasonings` work across styles.
- `application/commands.py`: typed `MealCommand`, `WorkflowCommand`, `ScrapeCommand`; structured
  allowlisted `key=value` input. Profiles/request overrides resolve without opening a user DB.
- Writer composition explicitly migrates offer DB; readers open it read-only. Control DB opens
  only when requested. Baseline adoption validates the existing schema and preserves history.
- Publication replaces the catalogue head atomically only after a successful nonempty run.
  Failed refreshes retain the last complete collection within the default 36-hour freshness limit,
  show warnings and hide expired/not-yet-valid offers. Price history/evidence are preserved.
- Reports pin a run and save immutable per-request inputs/catalog/report files; CLI `latest` is
  a local convenience. UUIDs do not authorize access; future server ownership checks are required.
- Daily collection defaults to **02:00 Europe/Prague**, configurable, DST handled. Not activated.
- Catalogue API is private and read-only, loopback by default, with health/status/filter endpoints.
  Docker roles are collector, catalogue API and on-demand CLI; base image excludes browser extras.

## Acquisition limits

- Kupi: 13 grocery categories, multiple retailers, advertised promotions; default meal source.
- Rohlík: public HTTP/JSON catalogue, 11 food/drink categories. Dated live produce collection
  succeeded; scope reflects the anonymous warehouse/locality, not a chosen delivery address.
- Tesco: isolated Playwright browser, ordinary departments, standard/Clubcard variants. Live
  produce page parsed; page two returned 403. Complete live acquisition remains blocked.
- Makro: customer session and selected branch, VAT-inclusive package quotations, sale minimums,
  increments and explicitly known item fees/deposits. Authenticated live pricing remains unverified.
- Missing charges are unknown, not zero; estimated weights do not imply guaranteed checkout costs.
  Meals rank consumed-ingredient cost, not whole-package checkout or basket delivery fees.
- Meal planning consumes one configured source/scope at a time; it does not combine source batches.

## Phase order and next work

Keep the agreed order: **1 foundation (done) → 1b meal styles (done) → 2 users/jobs →
3 browser → 4 configurable email → 5 SimpleX Chat**. Accounts/authentication, durable jobs,
public browser controls, email and SimpleX transport are not implemented.
Details: [server plan](server-access-plan.md), [phase 1](phase-one.md),
[catalogue/deployment](catalogue.md), [meals](meals.md), [adapter contract](adapter-contract.md).

## Verification

Combined tree on 2026-10-01: **441 offline tests passed**, Ruff lint/format passed (132 files),
mypy passed (89 source files). Python here is 3.14.7; project supports 3.13+.
The preceding foundation-only committed archive passed 334 tests. A native wheel was built.
Docker is absent locally; image/Compose execution is unverified. PostgreSQL has no live verification.
The sandbox blocks asyncio's internal wake-up socket; run offline pytest with approved escalation
if it stalls in `asyncio.to_thread`. No live retailer requests are needed for normal tests.

## Recent logical commits (oldest first)

```text
c912557 feat: add separate schema migrations and published grocery catalogue
f8376e2 feat: add reusable meal controls and independent catalogue collector
07c31cb chore: package collector and catalogue services for Docker
5c19c6b refactor: share robots policy across acquisition adapters
3b9383c feat: model purchase constraints and exact package costs
f2f9d95 feat: acquire Rohlik public catalogue with coverage checks
2c01c71 feat: add Tesco browser adapter and offline catalogue fixtures
01d1cf0 feat: add authenticated Makro assortment and pack pricing
fdb2b79 feat: register direct retailers and document acquisition setup
```

This handoff follows those commits. Check `git status` and `git log` before resuming work.
