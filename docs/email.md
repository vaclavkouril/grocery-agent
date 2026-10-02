# Optional email channel

Run `grocery-email --config config/email.toml --once` (or
`python -m grocery_agent.apps.email --config config/email.toml --once`) for one
bounded intake/delivery round. Omit `--once` to poll. All three switches default to
false: `enabled` is the master switch; `intake_enabled` and `delivery_enabled`
independently control IMAP intake and SMTP delivery. Disabled startup returns 0
without importing the channel client or connecting. Email uses standard-library
TLS mailbox transports; no extra package dependencies are required.

Configuration is flat TOML with `GROCERY_EMAIL_` environment overrides. Set hosts,
ports, sender, backend URL and enabled directions. `GROCERY_EMAIL_TRUSTED_SENDERS`
is a JSON array of exact mailbox addresses; booleans accept true/false or 1/0.
Mail servers use implicit TLS (normally IMAP 993 and SMTP 465), system certificate
verification and bounded socket timeouts.

Credentials are environment only: `GROCERY_EMAIL_SERVICE_TOKEN`,
`GROCERY_EMAIL_IMAP_USERNAME`, `GROCERY_EMAIL_IMAP_PASSWORD`,
`GROCERY_EMAIL_SMTP_USERNAME`, `GROCERY_EMAIL_SMTP_PASSWORD`.
Only the enabled direction's mailbox credentials are required. The service token
is the email channel credential matching the backend's
`GROCERY_BACKEND_EMAIL_TRANSPORT_TOKEN`. Configure it independently in each
service environment; do not pass user sessions or the other channel's service credential
to the transport. Secrets have no TOML fields; unknown fields are
rejected. This app does not load dotenv files. Provision secrets through the
service environment and protect that environment. Use an HTTPS backend URL when
the backend is not on the same trusted host.

## Sender trust is a deployment requirement

Enable intake only behind an ingress that strips forged, client-supplied
Authentication-Results headers and writes its own DMARC evaluation. Configure its
exact `trusted_authserv_id`, explicitly attest this setup with `trusted_ingress =
true`, and enumerate locally trusted sender addresses. A supplied header alone
cannot establish trust. DMARC authenticates the From domain, not the individual
mailbox: trust the domain's policy and its restrictions on who may use the allowed
address. No automatic pairing or new contacts are created by email.

The restrictive parser requires exactly one well-formed From mailbox, an exact
allowlisted address, exactly one Authentication-Results header from the configured
authserv-id, and exactly one aligned clause of the form
`dmarc=pass header.from=example.org`. Flat DMARC comments such as
`(p=REJECT sp=REJECT)` are accepted; extra DMARC properties, quoted domains, nested
comments, ambiguous headers and non-ASCII/quoted mailbox addresses are rejected.
Untrusted authserv-id headers never authorize a sender. Rejected messages remain
unseen for operator review, so put a quarantine/filter in front of a busy inbox.

Auto-submitted messages, List-* headers, bulk/list/junk precedence, own From and
own deterministic Message-ID are intentionally ignored and marked seen to avoid
loops. Intake accepts one inline plain-text body, takes its first nonempty line,
and never scans later lines for commands or follows quoted text. HTML-only,
attached text, nested mail and oversized messages are rejected. Configure byte,
command and batch limits as needed. Message text is never executed as a shell.

Request a binding through the authenticated [channel API](channels.md), then send
the delivered challenge as `verify TOKEN`. For example, send
`recipe provider=template servings=2 have=rice=500g budget=80` on its own first
line and follow the backend reply's `confirm TOKEN` instruction. Help, status,
cancel, binding, durable deduplication and confirmations are all handled by the
backend. The channel passes commands unchanged using the shared
`ChannelClient(api_url, token, "email")` contract.

IMAP events include a hash of account/server/mailbox plus UIDVALIDITY and UID;
Message-ID is never the deduplication key. BODY.PEEK leaves messages unseen until
backend intake returns processed, duplicate or ignored. Failures leave messages
unseen; a later retry relies on backend deduplication. Each round handles at most
`batch_size` unseen candidates, and checks RFC822.SIZE before fetching the body.
An IMAP failure does not prevent that round's SMTP work.

SMTP uses only the backend delivery address as envelope recipient. It emits a
stable Message-ID derived from delivery_id and `Auto-Submitted: auto-generated`.
The backend's text is the body; immutable report HTML and result JSON are attached
as `report.html` and `result.json`, with MIME encoding and fixed safe filenames.
There is no rendering or regeneration of the report. Delivery is acknowledged
only after SMTP accepts the recipient and message; send/construction failures
are nacked with a generic, secret-free error. If acceptance succeeds but the ack
fails, the lease can expire and cause a retry with the same Message-ID. SMTP does
not guarantee exactly-once delivery, and recipient systems may not deduplicate
that ID. `--once` returns 1 on a failed direction; continuous mode logs a generic
failure and retries after the configured polling interval.

Tests in `tests/test_email.py` use injected backend, inbox and mailer fixtures plus
stubbed stdlib SSL clients; they do not open network connections.
