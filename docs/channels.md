# Email and SimpleX channels

The optional [email application](email.md) and [SimpleX application](simplex.md) are separate
processes using the backend HTTP client. Both remain disabled by default; implementing them does
not connect a mailbox, pair a contact, start a daemon or send a notification. Recipe evaluation
still runs in the existing worker, with the same capability limits as website/CLI requests.

## Enable only the intended services

Back up the separate control database and upgrade it explicitly before starting the new backend:

```sh
grocery-agent db upgrade control
```

`control_0003` preserves existing accounts, jobs, bindings and queued notifications. It adds
durable intake events/confirmations and outbox leases. Offer history and the offer schema are
unchanged. No production migration is performed automatically. Include the control database,
private mailbox settings and externally operated SimpleX identity storage in reviewed backups.

Set `email_enabled` and/or `simplex_enabled` in backend configuration and provision separate
random credentials of 20..200 characters in `GROCERY_BACKEND_EMAIL_TRANSPORT_TOKEN` and
`GROCERY_BACKEND_SIMPLEX_TRANSPORT_TOKEN`. These credentials are environment-only; TOML rejects
them. A blank credential never enables a channel. Match the corresponding client credential
described in its quickstart. User bearer sessions cannot operate transports, and a transport
credential cannot browse catalogue data, use another channel, invite users or read arbitrary jobs.
The transport is trusted to assert verified sender identities, so protect its process/environment.

Configure each application separately in `config/email.toml` or `config/simplex.toml` with its own
environment prefix. Built-in defaults are overridden by that application's TOML, then environment.
Email's master, intake and delivery switches are independent; delivery-only operation can service
existing bindings. New bindings and confirmations need a functioning delivery path.
Keep `delivery_lease_seconds` longer than a complete send and backend acknowledgement round;
the default is 120 seconds. The default retry ceiling is `delivery_max_attempts = 5`.
Use TLS to reach a backend on another trusted machine. Recipe policies and sources remain bounded
by server capabilities; configured combined requests accept `source=kupi,tesco` and per-source
`profiles=SOURCE:FINGERPRINT,...`. Model allowlists and per-user quotas remain unchanged.
See [cache policies and combined-source recipes](recipe-service.md).

## Bind a channel to an existing account

Using an authenticated user session, request a proof-of-control challenge:

```sh
curl -H "Authorization: Bearer $GROCERY_API_TOKEN" -H 'Content-Type: application/json' \
  -d '{"channel":"email","address":"alice@example.org"}' \
  http://127.0.0.1:8001/v1/bindings
# For SimpleX, address is the paired direct contact's numeric ID, e.g. "42".
```

The response contains a binding ID, not the challenge. The enabled transport delivers the
challenge only to that address/contact. Reply through that channel with `verify TOKEN` within
15 minutes. Users cannot mark themselves verified over HTTP. Addresses cannot be reassigned to
another account; reissuing a challenge rotates the proof and invalidates pending confirmations.
Email also requires a locally allowlisted address with trusted ingress validation; SimpleX requires
a locally configured paired direct contact. Provision those restrictions before binding.

`GET /v1/bindings` lists only the current account's bindings. `DELETE /v1/bindings/{binding_id}`
revokes only an owned binding and discards queued deliveries. The shared `GroceryClient` and
`AsyncGroceryClient` also expose `bind`, `bindings` and `revoke_binding`. Website forms do not
currently manage bindings. Bootstrap/invitation operations remain available without the website.

## Commands and confirmation

```text
help
recipe provider=template servings=2 have=rice=500g budget=80.0000
confirm TOKEN
status JOB_UUID
cancel JOB_UUID
```

An optional `/` prefix is accepted. Recipe aliases are `meal` and `meals`; parameters include
provider/model, source/cache_policy, servings/style, pantry, budget, maximum stores and exclusions.
Unsupported or duplicate parameters are rejected, never silently discarded. The shared parser
returns the same `RecipeRequest` used by HTTP/CLI and retains Decimal quantities. Defaults come
from server capabilities, not from mailbox/chat configuration. `help` describes exact syntax.

A recipe command only stores an expiring confirmation and queues a summary of its complete
parameters. Reply `confirm TOKEN` within 15 minutes to create a job. Confirmations are bound to
the verified identity, one-use and durable; repeated confirmations return the same accepted job.
No incoming text is interpreted as a shell or bot control command. Private JSON/HTML reports are
delivered only to the originating binding; identifiers are never authorization tokens.

## Durable intake and delivery

`POST /v1/transports/{channel}/events` accepts a stable event ID, trusted address and bounded text.
The backend saves deduplication state, confirmation/job changes and reply outbox in one transaction.
Repeated events with different content are rejected. IMAP uses server/account/mailbox identity,
UIDVALIDITY and UID; SimpleX uses direct contact and immutable received item identities.

`POST /v1/transports/{channel}/outbox/claim` leases one allowed notification.
`POST /v1/transports/{channel}/outbox/{delivery_id}/ack` acknowledges with its lease token, or
records a generic transport failure. Expired leases recover after crashes; stale acknowledgements
cannot publish success. Retries back off up to five attempts by default. Disabled owners/revoked
bindings are checked before intake/delivery and worker execution/completion. Sent/failed/discarded
outbox payloads are cleared; event receipts retain private summaries for deduplication, so protect
the control DB and backups. Automatic retention cleanup is not implemented.

Delivery is at least once: SMTP or SimpleX may accept a send before the process loses its backend
acknowledgement. Retrying can deliver another copy. Email reuses a deterministic Message-ID, but
recipient systems need not deduplicate it. Revocation cannot retract a message already handed to
an external transport. Credentials and raw transport exceptions are not logged.

## Deployment and acceptance

Compose adds opt-in `email` and `simplex` profiles; it never starts or provisions a SimpleX daemon.
Enable the backend and each desired application explicitly after reviewing configuration/secrets.
The SimpleX service uses Linux host networking to access the externally configured loopback daemon;
its backend connection uses the published loopback backend port. Email uses the private Compose
backend service. Mailbox/provider/live paired-contact acceptance and Compose execution are separate
operator-configured checks. Offline tests use temporary databases, injected mailbox/WebSocket
fixtures and no real retail/model/mail/chat services.

After configuration, explicit Compose startup is, for example:

```sh
docker compose --profile backend --profile email up -d backend worker email
# Or the separately configured local chat bridge:
docker compose --profile backend --profile simplex up -d backend worker simplex
```

Compose reads channel secrets/switches from its environment and mounts the application TOML files.
Configure email hosts, sender and trusted ingress in that TOML. SimpleX exposes explicit daemon
URL/profile/contact allowlist environment settings; its daemon must remain a dedicated local
identity. No daemon/profile is bundled or automatically started by these commands.
