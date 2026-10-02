# Remaining acceptance and account setup

## Results (2026-10-02)

The final full offline/browser suite passed (1,013 tests), with two opt-in live tests deselected.
Ruff, mypy (126 files), Node contracts and wheel packaging passed. One upstream FastAPI/httpx
TestClient deprecation warning remains; it is not a test failure. Real Codex generation exposed an unsupported
Decimal regex in the provider schema; it was fixed and the real structured-output check passed.
Real Ollama generation passed with the installed `qwen2.5-coder:0.5b` after constraining ingredient
IDs in the schema. The 1.5b model exceeded the initial 60-second smoke limit, then passed with the
fixed schema and the normal 120-second timeout (71 seconds). Live Ollama tests now use that
application default; both installed qwen model sizes passed.
Live checks are explicit and excluded from the normal suite:

```sh
GROCERY_LIVE_CODEX=1 .venv/bin/pytest -q -m live tests/test_live_providers.py -k codex
GROCERY_LIVE_OLLAMA_MODEL=qwen2.5-coder:0.5b .venv/bin/pytest -q -m live tests/test_live_providers.py -k ollama
```

Codex uses the existing CLI login/configured model unless `GROCERY_LIVE_CODEX_MODEL` is set,
following the [official non-interactive interface](https://learn.chatgpt.com/docs/non-interactive-mode).
These checks send synthetic ingredient context, not private pantry or customer data.

## Public acquisition results

Produce-only native profiles were collected and published using temporary offer databases and
evidence under `/tmp/grocery-public-acceptance-WIwU3i`, not production storage:

| Source | Result | Scope/coverage |
| --- | --- | --- |
| Kupi | 219 offers accepted, 0 rejected/errors; 9 pages | Praha, `ovoce-a-zelenina` only; complete selected profile |
| Rohlík | 753 offers accepted, 0 rejected/errors; 713 products | Anonymous warehouse 8799 / Praha 8, category 300102000 only; complete selected profile |
| Tesco | Failed, 0 fetched/accepted; HTTP 403 fetching robots.txt | Produce page not requested; no bypass attempted |

Successful fingerprints: Kupi `22492af7eaba6a8f3a83cf76`, Rohlík `641ab368c27fad0f14696d32`.
These are not full grocery-category or delivery-address acceptance claims. Tesco access denial is
an external blocker, not a parser fix. Its diagnostic used temporary storage under
`/tmp/grocery-tesco-live-xz65fs`. Temporary captures may be discarded later; do not treat them as backups.

## Docker prerequisite requiring interactive sudo

Docker is installed, but its daemon is stopped and Compose is missing. Automated setup was
approved but could not proceed because sudo requires a password. On this Arch host run:

```sh
sudo pacman -S --needed docker-compose
sudo systemctl start docker
docker compose version
```

Then run the CI container smoke commands from `.github/workflows/ci.yml` against an isolated
project name, for example `docker compose -p grocery-acceptance ...`. This avoids the normal
deployment's named data volume. Do not run `down --volumes` against production. If Docker socket
access is restricted, run Docker commands with interactive sudo rather than weakening permissions.
Installing/starting Docker is not the same as enabling grocery production services.
Container image/Compose execution (including the Python 3.13 image) and optional live PostgreSQL
verification remain unrun. The local verified Python is 3.14; SQLite remains the default storage.
Ollama is reachable on loopback with existing local models; no model download was required.

## Where to add the deferred live account configuration

Keep secrets in the service/process environment, never TOML, Git or command arguments.
The channel applications do not automatically load `.env`; export credentials or use a protected
service EnvironmentFile. `.env.example` lists names only. Keep email/SimpleX disabled until ready.

- Email nonsecrets: `config/email.toml` (hosts, sender, trusted ingress/senders, backend URL,
  enabled intake/delivery). Backend switch: `config/backend.toml` → `email_enabled`.
  Environment: `GROCERY_EMAIL_IMAP_USERNAME`, `GROCERY_EMAIL_IMAP_PASSWORD`,
  `GROCERY_EMAIL_SMTP_USERNAME`, `GROCERY_EMAIL_SMTP_PASSWORD`, `GROCERY_EMAIL_SERVICE_TOKEN`.
  The service token must match `GROCERY_BACKEND_EMAIL_TRANSPORT_TOKEN` in the backend environment.
  Use a dedicated test mailbox and explicitly trusted ingress; see [email](email.md).
- SimpleX nonsecrets: `config/simplex.toml` → daemon `ws_url`, `user_id`, paired
  `allowed_contact_ids`, backend URL and `enabled`. Backend switch: `simplex_enabled`.
  Environment: `GROCERY_SIMPLEX_TRANSPORT_TOKEN`, matching
  `GROCERY_BACKEND_SIMPLEX_TRANSPORT_TOKEN`. Pair a designated test contact manually and verify
  the active profile before testing. No pairing/contact creation is automatic. See [SimpleX](simplex.md).
- Makro: create private browser state with `python -m grocery_agent.stores.makro.login`.
  Set `GROCERY_MAKRO_SESSION_PATH` to the resulting private file, plus a distinct
  `GROCERY_MAKRO_ACCOUNT_SCOPE` and exact `GROCERY_MAKRO_STORE_NAME`. Keep the session outside
  source/Git; the default is ignored `data/makro/session.json`. See [Makro setup](makro.md).

After configuration, migrate separate test databases, start a test backend/worker, prove channel
bindings, and run one confirmed recipe through the test mailbox/contact. Check receipt deduplication,
private report delivery and reconnect/retry behavior. Actual sending changes external state; use only
designated test recipients. Real customer credentials and production DBs are not needed for regressions.
