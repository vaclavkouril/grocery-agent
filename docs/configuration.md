# Configuration and portable applications

All runtime source is in `src/`: Python in `src/grocery_agent/`, browser code in `src/frontend/`.
Installed console commands work without changing into the checkout. Tests, editable configuration,
documentation and deployment files remain outside runtime source.

Configuration precedence is built-in defaults → shared TOML → application TOML → environment →
explicit request/CLI parameters. Backend capabilities still bound server-permitted requests.
Select shared configuration with `GROCERY_SHARED_CONFIG=/absolute/path/shared.toml` or application
`--shared-config`. Unknown tables/fields are rejected; credentials belong only in environment
variables, including transport tokens, mailbox passwords and credential-bearing URLs.

The shared file can contain `[shared]`, `[recipes]`, `[backend]`, `[scrape]`, `[collect]`, `[email]`
and `[simplex]` tables. Application files contain their own fields at the top level, as in `config/`.
Paths in shared TOML resolve relative to the shared file; explicit application TOML paths resolve
relative to that application file. Environment and explicit CLI paths follow their normal meanings.

```toml
[shared]
database_url = "sqlite:///state/offers.db"
snapshot_dir = "state/snapshots"
report_dir = "state/reports"
lock_path = "state/acquisition.lock"
meal_config = "meals.toml"

[recipes]
default_provider = "codex"
default_cache_policy = "local"

[backend]
providers = ["codex", "template"]
cache_policies = ["cache-only"]
```

Use a separate control database (`GROCERY_CONTROL_DATABASE_URL`) for accounts/jobs. Runtime data
must live in a writable location, not the installed package. Application default discovery checks
`config/<app>.toml` in the current directory, then bundled wheel examples, then an absolute checkout
fallback. The ingredient/meal registry is bundled too. Explicit `--config` paths must exist.

For an installation outside the checkout:

```sh
export GROCERY_SHARED_CONFIG=/srv/grocery/shared.toml
export GROCERY_CONTROL_DATABASE_URL=sqlite:////srv/grocery/state/control.db
grocery-agent db upgrade offers
grocery-agent db upgrade control
grocery-recipes --shared-config /srv/grocery/shared.toml --cache-only --provider template
grocery-backend --shared-config /srv/grocery/shared.toml --config /srv/grocery/backend.toml serve
```

These are operator commands, not automatic service activation. Keep API/worker configuration in
sync. `grocery-email` and `grocery-simplex` independently load their own files and remain disabled
by default. Retailer adapter environment variables and the existing `grocery-agent` commands remain
supported. See each application's `--help`, [recipes](recipe-service.md), [backend](backend.md) and
[channels](channels.md) for parameter references and transport-specific requirements.
