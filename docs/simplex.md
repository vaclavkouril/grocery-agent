# SimpleX transport

The adapter is disabled by default. Run `grocery-simplex --config config/simplex.toml`
or `.venv/bin/python -m grocery_agent.apps.simplex --config config/simplex.toml`
from the checkout. Disabled startup opens no HTTP or WebSocket
connection and does not import the optional `websockets` dependency. Enabled startup
requires the optional `simplex` extra (`websockets` providing `websockets.sync.client`)
and the shared backend `ChannelClient` implementation.

Configure and pair the daemon profile externally, then run the local CLI daemon with
`simplex-chat -p 5225`. Set `user_id` to that profile and `allowed_contact_ids` to its
already paired numeric direct-contact IDs. The adapter does not create profiles,
public addresses, contacts, pairings, or auto-accept rules. Do not reuse these IDs with
a different daemon database; update backend bindings when replacing the profile.
On Linux, the opt-in Compose transport uses host networking to reach this externally
managed daemon on localhost port 5225 and the published backend on localhost port 8001.

Enable `enabled` in TOML or `GROCERY_SIMPLEX_ENABLED=true`. Every non-secret TOML field
has an uppercase `GROCERY_SIMPLEX_` environment override; numeric and boolean values
use JSON, and `GROCERY_SIMPLEX_ALLOWED_CONTACT_IDS='[2,3]'` sets the allowlist.
Set `GROCERY_SIMPLEX_TRANSPORT_TOKEN` only in the process environment to the separately
provisioned backend channel-service credential. User session tokens are not transport
credentials. TOML secret fields and credentials embedded in endpoint URLs are rejected.
Only loopback WebSocket endpoints are accepted; there is no remote endpoint override.
Environment WebSocket proxies are disabled to keep daemon traffic on loopback.
The backend separately enables SimpleX and configures
`GROCERY_BACKEND_SIMPLEX_TRANSPORT_TOKEN`; give the app's environment-only service token
the corresponding credential value. The adapter does not read the backend's environment
variable or a user session credential.

Compose configuration names: mount `config/simplex.toml`, pass it using `--config`,
set app `GROCERY_SIMPLEX_ENABLED=true`, `GROCERY_SIMPLEX_API_URL=http://127.0.0.1:8001`,
`GROCERY_SIMPLEX_WS_URL=ws://127.0.0.1:5225`, `GROCERY_SIMPLEX_USER_ID=1`,
`GROCERY_SIMPLEX_ALLOWED_CONTACT_IDS='[42]'`, and `GROCERY_SIMPLEX_TRANSPORT_TOKEN`.
`GROCERY_SIMPLEX_SERVICE_TOKEN` is also accepted as an environment-only compatibility
alias; a nonempty `GROCERY_SIMPLEX_TRANSPORT_TOKEN` takes precedence.
The backend uses `GROCERY_BACKEND_SIMPLEX_ENABLED=true` and
`GROCERY_BACKEND_SIMPLEX_TRANSPORT_TOKEN`. Timing overrides are
`GROCERY_SIMPLEX_POLL_SECONDS`, `GROCERY_SIMPLEX_RESPONSE_TIMEOUT`,
`GROCERY_SIMPLEX_RECONNECT_SECONDS`, and `GROCERY_SIMPLEX_RECONNECT_MAX_SECONDS`.
`GROCERY_SIMPLEX_HISTORY_COUNT` overrides the per-contact recent history window.
Startup diagnostics are generic and runtime logs contain exception class names only;
message text and confirmation/verification tokens are never logged.

The [official bot API](https://github.com/simplex-chat/simplex-chat/blob/stable/bots/README.md)
defines JSON envelopes and an unauthenticated localhost daemon. The adapter uses
[`/_send` with JSON composed messages](https://github.com/simplex-chat/simplex-chat/blob/stable/bots/api/COMMANDS.md#apisendmessages)
and [direct received text item types](https://github.com/simplex-chat/simplex-chat/blob/stable/bots/api/TYPES.md).
Message text is JSON data and is never executed as shell or daemon control commands.
Groups, outgoing items, files, unknown events, unapproved contacts, other profiles, and
correlated response frames are excluded from intake.
The only correlated frames used for intake are responses to the adapter's own
bounded history requests, after checking their profile and direct contact identity.

Backend sender addresses are numeric contact ID strings such as `"42"`. Event keys are
`simplex:USER_ID:CONTACT_ID:CHAT_ITEM_ID` (at most 67 characters for int64 IDs), scoped
to one externally configured daemon/profile. Deduplication, account bindings,
confirmations and outbox leases all
belong to the backend. Intake replies are delivered through its durable outbox.
Send the UI link challenge as `verify TOKEN`. Start a recipe with
`recipe provider=template servings=2 have=rice=500g budget=80`, then send the returned
`confirm TOKEN`. `help`, `status`, and `cancel` are also passed unchanged to the backend.

Only a matching correlated `newChatItems` send response acknowledges an outbox lease.
This means the daemon accepted the send, not that the recipient read it. Command errors
negative-acknowledge the lease. Disconnects and response deadlines negative-acknowledge
ambiguous sends and reconnect with exponential delay capped by `reconnect_max_seconds`.
Delivery is at-least-once: a daemon may accept a message before its response is lost,
so retry can deliver a duplicate. If backend acknowledgement fails, lease expiry provides
recovery. Failed backend intake remains in a process-memory queue (up to 4,096 events
and 8 MiB of text) and is retried with the original ID across reconnects. A whole batch
is queued before submission, preserving later items if the first request fails.

Every initial connection and reconnect verifies the configured active profile with
[`/user` and `/_contacts USER_ID`](https://github.com/simplex-chat/simplex-chat/blob/stable/bots/api/COMMANDS.md)
and fails closed if any allowlisted contact is absent. It then replays up to
`history_count` recent items (default 50, range 1–200) from each allowed direct chat with
`/_get chat @CONTACT_ID count=N`. The exact history command and `apiChat` response are
defined in the official [command parser](https://github.com/simplex-chat/simplex-chat/blob/stable/src/Simplex/Chat/Library/Commands.hs)
and [response types](https://github.com/simplex-chat/simplex-chat/blob/stable/src/Simplex/Chat/Controller.hs).
The active profile is also rechecked with `/user` immediately before each outbox send.
A mismatch refuses the send and releases its lease for retry; no profile switch is
issued. Operate a dedicated daemon/profile: another controller must not change the
active profile between this check and the send command, which are separate API calls.
Incoming received text is replayed oldest-first using the same stable backend event IDs;
outgoing, edited, live, deleted, group and file items are skipped. History/control
responses never acknowledge delivery leases. Backend deduplication makes overlapping
history and live events safe. No persistent local cursor or spool is created. After a
process crash, items outside the recent window (including outgoing items in its count),
or removed/expired from daemon history, cannot be recovered and must be resent.

`--once` verifies the profile and reconciles history, then receives one frame (or waits
one polling timeout), claims at most one outbox
delivery, waits for its correlated response while handling interleaved incoming events,
then exits. It returns 1 on connection/response failure and does not reconnect. Offline
tests inject the socket and frozen backend client interface; no daemon is required.

Completion deliveries render the backend recipe report as plain text: titles, servings,
time, steps, per-serving macros and usage cost, and ingredient use/owned/buy grams with
retailer. Meal alternatives are not a combined shopping plan. No report HTML, offer URLs,
or authenticated backend download links are sent. The text payload is capped at 8,000
UTF-8 bytes with an explicit truncation notice; individual fields are capped at 600
characters, with at most eight meals, sixteen steps and thirty-two ingredients per meal.
No local files are created for delivery. No-feasible-recipe results include their reason.
