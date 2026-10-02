"""Offline email trust, replay, acknowledgment and independent startup checks."""

import json
import ssl
from collections import deque
from contextlib import nullcontext
from dataclasses import replace
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path
from typing import Any

import httpx
import pytest

from grocery_agent.apps import email as app
from grocery_agent.channels import email
from grocery_agent.http_client import ChannelClient


@pytest.fixture
def config() -> email.EmailConfig:
    return email.EmailConfig(
        enabled=True,
        intake_enabled=True,
        delivery_enabled=True,
        imap_host="imap.example.org",
        smtp_host="smtp.example.org",
        sender="agent@example.org",
        trusted_authserv_id="mx.example.org",
        trusted_ingress=True,
        trusted_senders=("alice@example.net",),
        batch_size=2,
    )


@pytest.fixture
def credentials() -> email.EmailCredentials:
    return email.EmailCredentials(
        "service-secret", "imap-user", "imap-secret", "smtp-user", "smtp-secret"
    )


def mail(
    text: str = "recipe provider=template servings=2 have=rice=500g budget=80\n\n> cancel old-job",
    *,
    sender: str = "Alice <alice@example.net>",
    auth: str | None = "mx.example.org; spf=pass smtp.mailfrom=example.net; "
    "dmarc=pass (p=REJECT sp=REJECT) header.from=example.net",
    headers: dict[str, str] | None = None,
) -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "agent@example.org"
    message["Message-ID"] = "<reused-id@example.net>"
    if auth is not None:
        message["Authentication-Results"] = auth
    for name, value in (headers or {}).items():
        message[name] = value
    message.set_content(text)
    return message.as_bytes()


class Backend:
    def __init__(self, deliveries: list[dict[str, Any]] | None = None) -> None:
        self.events: list[tuple[str, str, str]] = []
        self.acks: list[tuple[str, str, str | None]] = []
        self.deliveries = deque(deliveries or [])
        self.status = "processed"
        self.fail_intake = False
        self.fail_ack = False

    def intake(self, event_id: str, address: str, text: str) -> dict[str, Any]:
        self.events.append((event_id, address, text))
        if self.fail_intake:
            raise RuntimeError("private backend credential")
        return {"status": self.status, "reply": "confirm TOKEN"}

    def claim(self) -> dict[str, Any] | None:
        return self.deliveries.popleft() if self.deliveries else None

    def ack(self, delivery_id: str, lease_token: str, *, error: str | None = None) -> None:
        self.acks.append((delivery_id, lease_token, error))
        if self.fail_ack:
            raise RuntimeError("secret ack error")


class Inbox:
    uidvalidity = "120"

    def __init__(self, messages: list[tuple[str, bytes]]) -> None:
        self.rows = messages
        self.seen: list[str] = []

    def messages(self, limit: int, max_bytes: int) -> Any:
        yield from self.rows[:limit]

    def mark_seen(self, uid: str) -> None:
        self.seen.append(uid)


class Mailer:
    def __init__(self, backend: Backend, *, fail: bool = False) -> None:
        self.backend = backend
        self.fail = fail
        self.sent: list[tuple[EmailMessage, str, str]] = []

    def send(self, message: EmailMessage, sender: str, recipient: str) -> None:
        assert not self.backend.acks  # SMTP acceptance must precede acknowledgment.
        if self.fail:
            raise RuntimeError("smtp-password=do-not-leak")
        self.sent.append((message, sender, recipient))


def delivery(**overrides: Any) -> dict[str, Any]:
    return {
        "delivery_id": "job-123/result",
        "lease_token": "lease-secret",
        "address": "alice@example.net",
        "text": "Your recipe is ready",
        "subject": "Recipe ready",
        "report_html": "<html>Česnek<script>unmodified()</script></html>",
        "result": {"servings": 2, "name": "Česnek"},
        **overrides,
    }


def test_first_line_identity_and_backend_owned_commands(config: email.EmailConfig) -> None:
    text = "recipe provider=template servings=2 have=rice=500g budget=80"
    assert email.extract_command(mail(), config) == ("alice@example.net", text)
    for command in ("verify TOKEN", "confirm TOKEN", "help", "status job-1", "cancel job-1"):
        assert email.extract_command(mail("\n" + command + "\n> recipe evil"), config) == (
            "alice@example.net",
            command,
        )


@pytest.mark.parametrize(
    "auth",
    [
        None,
        "evil.example.org; dmarc=pass header.from=example.net",
        "mx.example.org; dmarc=fail header.from=example.net",
        "mx.example.org; dmarc=pass header.from=other.example.net",
        "mx.example.org; dmarc=pass header.from=example.net.evil.org",
        'mx.example.org; dmarc=pass header.from="example.net"',
        "mx.example.org; dmarc=pass header.from=example.net extra=yes",
        "mx.example.org; dmarc=pass (outer (nested)) header.from=example.net",
        "mx.example.org; dmarc=pass header.from=example.net; dmarc=fail header.from=example.net",
        "mx.example.org; dkim=pass header.d=example.net",
    ],
)
def test_authentication_fail_closed(config: email.EmailConfig, auth: str | None) -> None:
    assert email.extract_command(mail(auth=auth), config) is None


@pytest.mark.parametrize(
    "sender",
    [
        "alice@example.net, bob@example.net",
        "Mallory <mallory@example.net>",
        '"alice spoof"@example.net',
        "alice@evil.example.net",
        "invalid mailbox",
    ],
)
def test_sender_fail_closed(config: email.EmailConfig, sender: str) -> None:
    assert email.extract_command(mail(sender=sender), config) is None


def test_duplicate_from_and_trusted_auth_headers(config: email.EmailConfig) -> None:
    raw = mail()
    assert email.extract_command(b"From: alice@example.net\n" + raw, config) is None
    assert (
        email.extract_command(
            b"Authentication-Results: mx.example.org; dmarc=pass header.from=example.net\n" + raw,
            config,
        )
        is None
    )
    # An unrelated authserv-id does not replace the trusted verdict.
    assert email.extract_command(b"Authentication-Results: other; dmarc=fail\n" + raw, config)


@pytest.mark.parametrize(
    "headers",
    [
        {"Auto-Submitted": "auto-generated"},
        {"Auto-Submitted": "auto-replied"},
        {"List-Id": "some-list.example.net"},
        {"List-Unsubscribe": "<mailto:list@example.net>"},
        {"Precedence": "bulk"},
        {"Precedence": "list"},
        {"Precedence": "junk"},
    ],
)
def test_local_loop_and_junk_seen_without_backend(
    config: email.EmailConfig,
    credentials: email.EmailCredentials,
    headers: dict[str, str],
) -> None:
    inbox = Inbox([("1", mail(headers=headers))])
    backend = Backend()
    assert email.intake_once(config, credentials, backend, inbox) == 0
    assert inbox.seen == ["1"]
    assert backend.events == []


def test_self_from_and_self_message_id(config: email.EmailConfig) -> None:
    assert email.extract_command(mail(sender=config.sender, auth=None), config) == "junk"
    raw = mail().replace(
        b"<reused-id@example.net>",
        str(email.build_delivery(config, delivery())["Message-ID"]).encode(),
    )
    assert email.extract_command(raw, config) == "junk"


def test_mime_and_size_rejection(config: email.EmailConfig) -> None:
    message = BytesParser(policy=policy.default).parsebytes(mail())
    message.set_content("<p>verify BAD</p>", subtype="html")
    assert email.extract_command(message.as_bytes(), config) is None
    message.set_content("help")
    message.add_attachment("confirm BAD", subtype="plain", filename="command.txt")
    assert email.extract_command(message.as_bytes(), config) == ("alice@example.net", "help")
    attached = BytesParser(policy=policy.default).parsebytes(mail("verify BAD"))
    message.add_attachment(attached)
    assert email.extract_command(message.as_bytes(), config) is None
    message = BytesParser(policy=policy.default).parsebytes(mail())
    message.make_alternative()
    message.add_alternative("verify BAD", subtype="plain")
    assert email.extract_command(message.as_bytes(), config) is None
    assert email.extract_command(mail("> confirm OLD\nverify GOOD"), config) is None
    assert email.extract_command(mail(""), config) is None
    assert email.extract_command(mail("help\x00bad"), config) is None
    assert email.extract_command(mail("x" * 4100), config) is None
    assert email.extract_command(mail(), replace(config, max_message_bytes=50)) is None


@pytest.mark.parametrize("status", ["processed", "duplicate", "ignored"])
def test_seen_after_backend_acceptance_and_uid_identity(
    config: email.EmailConfig,
    credentials: email.EmailCredentials,
    status: str,
) -> None:
    backend = Backend()
    backend.status = status
    inbox = Inbox([("1", mail()), ("2", mail()), ("3", mail())])
    assert email.intake_once(config, credentials, backend, inbox) == 0
    assert inbox.seen == ["1", "2"]
    first, second = [row[0] for row in backend.events]
    assert first != second
    assert first == email.event_id(config, credentials, "120", "1")
    assert first != email.event_id(config, credentials, "121", "1")
    assert first != email.event_id(replace(config, imap_mailbox="Other"), credentials, "120", "1")
    assert "imap-user" not in first
    inbox.seen.clear()
    backend.status = "duplicate"
    email.intake_once(config, credentials, backend, inbox)
    assert backend.events[2][0] == first


def test_failed_and_untrusted_intake_remains_unseen(
    config: email.EmailConfig,
    credentials: email.EmailCredentials,
) -> None:
    inbox = Inbox([("1", mail()), ("2", mail(auth=None))])
    backend = Backend()
    backend.fail_intake = True
    assert email.intake_once(config, credentials, backend, inbox) == 1
    assert inbox.seen == []
    backend.fail_intake = False
    backend.status = "invalid"
    assert email.intake_once(config, credentials, backend, inbox) == 1
    assert inbox.seen == []


def test_delivery_attachments_envelope_and_deterministic_id(config: email.EmailConfig) -> None:
    item = delivery()
    original = json.dumps(item, ensure_ascii=False)
    backend = Backend([item])
    mailer = Mailer(backend)
    assert email.delivery_once(config, backend, mailer) == 0
    assert backend.acks == [(item["delivery_id"], item["lease_token"], None)]
    message, sender, recipient = mailer.sent[0]
    assert sender == config.sender and recipient == item["address"]
    assert message["To"] == item["address"]
    assert message["Auto-Submitted"] == "auto-generated"
    assert message["Message-ID"] == email.build_delivery(config, item)["Message-ID"]
    assert (
        message["Message-ID"]
        != email.build_delivery(config, delivery(delivery_id="other"))["Message-ID"]
    )
    roundtrip = BytesParser(policy=policy.default).parsebytes(message.as_bytes())
    attachments = list(roundtrip.iter_attachments())
    assert [part.get_filename() for part in attachments] == ["report.html", "result.json"]
    assert attachments[0].get_payload(decode=True).decode() == item["report_html"]
    assert json.loads(attachments[1].get_payload(decode=True)) == item["result"]
    assert json.dumps(item, ensure_ascii=False) == original


@pytest.mark.parametrize(
    "override",
    [
        {"address": "alice@example.net\r\nBcc: evil@example.net"},
        {"address": "alice@example.net, evil@example.net"},
        {"subject": "injected\nBcc: evil@example.net"},
        {"result": {"bad": float("nan")}},
    ],
)
def test_bad_backend_delivery_nacked_safely(
    config: email.EmailConfig, override: dict[str, Any]
) -> None:
    backend = Backend([delivery(**override)])
    mailer = Mailer(backend)
    assert email.delivery_once(config, backend, mailer) == 1
    assert not mailer.sent
    assert backend.acks[0][2] == "Email delivery failed"


def test_smtp_failure_and_ack_failure(config: email.EmailConfig) -> None:
    backend = Backend([delivery()])
    assert email.delivery_once(config, backend, Mailer(backend, fail=True)) == 1
    assert backend.acks[0][2] == "Email delivery failed"
    backend = Backend([delivery()])
    backend.fail_ack = True
    mailer = Mailer(backend)
    with pytest.raises(RuntimeError):
        email.delivery_once(config, backend, mailer)
    assert len(mailer.sent) == 1
    assert len(backend.acks) == 1 and backend.acks[0][2] is None


def test_delivery_round_bound_and_optional_attachments(config: email.EmailConfig) -> None:
    backend = Backend([delivery(), delivery(), delivery()])
    sent = []

    class Sink:
        def send(self, *args: Any) -> None:
            sent.append(args)

    assert email.delivery_once(config, backend, Sink()) == 0
    assert len(sent) == 2 and len(backend.deliveries) == 1
    assert not email.build_delivery(config, delivery(report_html=None, result=None)).is_multipart()


def test_config_environment_override_secret_exclusion(tmp_path: Path) -> None:
    path = tmp_path / "email.toml"
    path.write_text("enabled = false\nbatch_size = 3\n")
    config = email.EmailConfig.load(
        path,
        environ={
            "GROCERY_EMAIL_BATCH_SIZE": "7",
            "GROCERY_EMAIL_TRUSTED_SENDERS": '["alice@example.net"]',
            "GROCERY_EMAIL_IMAP_PASSWORD": "secret",
        },
    )
    assert config.batch_size == 7 and config.trusted_senders == ("alice@example.net",)
    assert not hasattr(config, "imap_password")
    for field in (
        "service_token",
        "imap_username",
        "imap_password",
        "smtp_username",
        "smtp_password",
    ):
        path.write_text(f'{field} = "secret"\n')
        with pytest.raises(ValueError, match="credentials belong in environment"):
            email.EmailConfig.load(path, environ={})


@pytest.mark.parametrize(
    "kwargs",
    [
        {"trusted_ingress": False},
        {"trusted_authserv_id": ""},
        {"trusted_senders": ()},
        {"sender": "two@example.org, three@example.org"},
        {"batch_size": 0},
        {"enabled": "false"},
    ],
)
def test_config_rejects_unsafe_intake(config: email.EmailConfig, kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        replace(config, **kwargs)


def test_disabled_main_never_connects(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in tuple(email.os.environ):
        if key.startswith("GROCERY_EMAIL_"):
            monkeypatch.delenv(key)

    def forbidden(*args: Any) -> Any:
        pytest.fail("disabled email opened a transport")

    assert (
        app.main(
            ["--once"], client_factory=forbidden, imap_factory=forbidden, smtp_factory=forbidden
        )
        == 0
    )
    assert app.main(["--config", "config/email.toml", "--once"], client_factory=forbidden) == 0
    monkeypatch.setenv("GROCERY_EMAIL_ENABLED", "true")
    assert app.main(["--once"], client_factory=forbidden) == 0


def setup_environment(monkeypatch: pytest.MonkeyPatch, *, intake: bool, delivery: bool) -> None:
    values = {
        "ENABLED": "true",
        "INTAKE_ENABLED": str(intake).lower(),
        "DELIVERY_ENABLED": str(delivery).lower(),
        "SENDER": "agent@example.org",
        "IMAP_HOST": "imap.example.org",
        "SMTP_HOST": "smtp.example.org",
        "TRUSTED_AUTHSERV_ID": "mx.example.org",
        "TRUSTED_INGRESS": "true",
        "TRUSTED_SENDERS": '["alice@example.net"]',
        "SERVICE_TOKEN": "service-secret",
    }
    for key, value in values.items():
        monkeypatch.setenv("GROCERY_EMAIL_" + key, value)
    for direction, active in (("IMAP", intake), ("SMTP", delivery)):
        for suffix in ("USERNAME", "PASSWORD"):
            key = f"GROCERY_EMAIL_{direction}_{suffix}"
            if active:
                monkeypatch.setenv(key, direction + "-secret")
            else:
                monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize("intake,delivery_enabled", [(True, False), (False, True), (True, True)])
def test_independent_directions_and_once(
    monkeypatch: pytest.MonkeyPatch,
    intake: bool,
    delivery_enabled: bool,
) -> None:
    setup_environment(monkeypatch, intake=intake, delivery=delivery_enabled)
    backend = Backend([delivery()])
    inbox = Inbox([("1", mail("verify TOKEN"))])
    mailer = Mailer(backend)
    opened: list[str] = []

    def client_factory(url: str, token: str, channel: str) -> Any:
        assert (url, token, channel) == ("http://127.0.0.1:8001", "service-secret", "email")
        return nullcontext(backend)

    def imap_factory(*args: Any) -> Any:
        opened.append("imap")
        return nullcontext(inbox)

    def smtp_factory(*args: Any) -> Any:
        opened.append("smtp")
        return nullcontext(mailer)

    assert (
        app.main(
            ["--once"],
            client_factory=client_factory,
            imap_factory=imap_factory,
            smtp_factory=smtp_factory,
            sleep=lambda _: pytest.fail("once slept"),
        )
        == 0
    )
    assert opened == (["imap"] if intake else []) + (["smtp"] if delivery_enabled else [])
    assert bool(backend.events) == intake and bool(mailer.sent) == delivery_enabled


def test_intake_failure_does_not_block_delivery_and_logs_are_secret_free(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    setup_environment(monkeypatch, intake=True, delivery=True)
    backend = Backend([delivery()])
    mailer = Mailer(backend)

    def failing(*args: Any) -> Any:
        raise RuntimeError("IMAP-secret")

    assert (
        app.main(
            ["--once"],
            client_factory=lambda *args: nullcontext(backend),
            imap_factory=failing,
            smtp_factory=lambda *args: nullcontext(mailer),
        )
        == 1
    )
    assert len(mailer.sent) == 1
    assert capsys.readouterr().err == "Email transport round failed\n"


def test_continuous_main_waits_between_bounded_rounds(monkeypatch: pytest.MonkeyPatch) -> None:
    setup_environment(monkeypatch, intake=True, delivery=False)
    inbox = Inbox([("1", mail("help"))])
    waits = []

    def stop(seconds: float) -> None:
        waits.append(seconds)
        raise KeyboardInterrupt

    assert (
        app.main(
            [],
            client_factory=lambda *args: nullcontext(Backend()),
            imap_factory=lambda *args: nullcontext(inbox),
            sleep=stop,
        )
        == 0
    )
    assert waits == [30.0] and inbox.seen == ["1"]


def test_missing_credentials_fail_without_opening_network(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    setup_environment(monkeypatch, intake=True, delivery=False)
    monkeypatch.delenv("GROCERY_EMAIL_IMAP_PASSWORD")
    assert app.main(["--once"], client_factory=lambda *args: pytest.fail("opened client")) == 1
    assert "secret" not in capsys.readouterr().err


def test_stdlib_imap_peek_sizes_uidvalidity_seen_and_ssl(
    config: email.EmailConfig,
    credentials: email.EmailCredentials,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    class Connection:
        def login(self, *args: Any) -> None:
            assert args == (credentials.imap_username, credentials.imap_password)

        def select(self, mailbox: str) -> Any:
            assert mailbox == "INBOX"
            return "OK", [b"2"]

        def response(self, key: str) -> Any:
            assert key == "UIDVALIDITY"
            return "UIDVALIDITY", [b"120"]

        def uid(self, *args: Any) -> Any:
            calls.append(args)
            if args[0] == "SEARCH":
                return "OK", [b"1 2 3"]
            if args[0] == "STORE":
                return "OK", []
            if args[2] == "(UID RFC822.SIZE)":
                size = len(mail()) if args[1] == "1" else config.max_message_bytes + 1
                return "OK", [f"7 (UID {args[1]} RFC822.SIZE {size})".encode()]
            assert args[2] == "(UID BODY.PEEK[])"
            return "OK", [(b"7 (UID 1 BODY[] {123}", mail()), b")"]

        def logout(self) -> None:
            calls.append(("logout",))

    def factory(host: str, port: int, *, ssl_context: ssl.SSLContext, timeout: float) -> Any:
        assert (host, port) == (config.imap_host, 993)
        assert ssl_context.verify_mode == ssl.CERT_REQUIRED and ssl_context.check_hostname
        assert timeout == 30
        return Connection()

    monkeypatch.setattr(email.imaplib, "IMAP4_SSL", factory)
    with email.IMAPTransport(config, credentials) as inbox:
        assert inbox.uidvalidity == "120"
        assert list(inbox.messages(2, config.max_message_bytes)) == [("1", mail())]
        assert not any(call[0] == "STORE" for call in calls)
        inbox.mark_seen("1")
    assert ("STORE", "1", "+FLAGS.SILENT", "(\\Seen)") in calls
    assert not any(call[0] == "FETCH" and call[1] == "3" for call in calls)
    assert not any(call[0] == "FETCH" and call[1] == "2" and "PEEK" in call[2] for call in calls)
    assert calls[-1] == ("logout",)


@pytest.mark.parametrize("refused", [False, True])
def test_stdlib_smtp_verified_ssl_explicit_envelope_and_refusal(
    config: email.EmailConfig,
    credentials: email.EmailCredentials,
    monkeypatch: pytest.MonkeyPatch,
    refused: bool,
) -> None:
    calls = []

    class Connection:
        def login(self, *args: Any) -> None:
            assert args == (credentials.smtp_username, credentials.smtp_password)

        def send_message(
            self, message: EmailMessage, *, from_addr: str, to_addrs: list[str]
        ) -> Any:
            calls.append((from_addr, to_addrs))
            return {to_addrs[0]: (550, b"refused secret")} if refused else {}

        def quit(self) -> None:
            calls.append("quit")

    def factory(host: str, port: int, *, context: ssl.SSLContext, timeout: float) -> Any:
        assert (host, port, timeout) == (config.smtp_host, 465, 30)
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
        return Connection()

    monkeypatch.setattr(email.smtplib, "SMTP_SSL", factory)
    with email.SMTPTransport(config, credentials) as mailer:
        if refused:
            with pytest.raises(RuntimeError, match="SMTP recipient not accepted"):
                mailer.send(
                    email.build_delivery(config, delivery()), config.sender, "alice@example.net"
                )
        else:
            mailer.send(
                email.build_delivery(config, delivery()), config.sender, "alice@example.net"
            )
    assert calls == [(config.sender, ["alice@example.net"]), "quit"]


def test_domain_normalization_preserves_local_part(config: email.EmailConfig) -> None:
    config = replace(config, trusted_senders=("Alice@Example.NET",))
    assert config.trusted_senders == ("Alice@example.net",)
    assert email.extract_command(mail("verify TOKEN", sender="Alice@EXAMPLE.NET"), config) == (
        "Alice@example.net",
        "verify TOKEN",
    )
    assert email.extract_command(mail(sender="alice@EXAMPLE.NET"), config) is None


@pytest.mark.parametrize(
    "sender",
    [
        '"alice"@example.net',
        'Alice <"alice"@example.net>',
        "alice(comment)@example.net",
        "Friends: alice@example.net;",
    ],
)
def test_ambiguous_raw_mailboxes_rejected(config: email.EmailConfig, sender: str) -> None:
    assert email.extract_command(mail(sender=sender), config) is None


@pytest.mark.parametrize("raw", ['"alice@example.net"', '{"alice@example.net":true}', "null"])
def test_trusted_sender_environment_requires_array(raw: str) -> None:
    with pytest.raises(ValueError):
        email.EmailConfig.load(environ={"GROCERY_EMAIL_TRUSTED_SENDERS": raw})


def test_seen_store_failure_replays_same_backend_event(
    config: email.EmailConfig,
    credentials: email.EmailCredentials,
) -> None:
    backend = Backend()

    class FailingInbox(Inbox):
        def mark_seen(self, uid: str) -> None:
            raise RuntimeError("store failed")

    assert email.intake_once(config, credentials, backend, FailingInbox([("1", mail())])) == 1
    backend.status = "duplicate"
    inbox = Inbox([("1", mail())])
    assert email.intake_once(config, credentials, backend, inbox) == 0
    assert backend.events[0] == backend.events[1] and inbox.seen == ["1"]


def test_email_with_real_shared_client_offline_http_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup_environment(monkeypatch, intake=True, delivery=True)
    calls = []
    item = delivery(delivery_id="email-delivery-1")
    backend = Backend()
    mailer = Mailer(backend)
    inbox = Inbox([("1", mail("verify TOKEN"))])
    claimed = False

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal claimed
        path = request.url.path
        calls.append(path)
        assert request.headers["Authorization"] == "Bearer service-secret"
        assert request.method == "POST"
        if path.endswith("/events"):
            payload = json.loads(request.content)
            assert payload["address"] == "alice@example.net"
            assert payload["text"] == "verify TOKEN" and payload["event_id"].endswith(":120:1")
            return httpx.Response(
                200, json={"status": "processed", "reply": "DO NOT SEND DIRECTLY"}
            )
        if path.endswith("/claim"):
            assert inbox.seen == ["1"]
            if claimed:
                return httpx.Response(204)
            claimed = True
            return httpx.Response(200, json=item)
        if path.endswith("/ack"):
            assert len(mailer.sent) == 1
            assert json.loads(request.content) == {"lease_token": "lease-secret", "error": None}
            return httpx.Response(204)
        pytest.fail(f"unexpected transport route: {path}")

    def factory(url: str, token: str, channel: str) -> ChannelClient:
        return ChannelClient(url, token, channel, transport=httpx.MockTransport(handle))

    assert (
        app.main(
            ["--once"],
            client_factory=factory,
            imap_factory=lambda *args: nullcontext(inbox),
            smtp_factory=lambda *args: nullcontext(mailer),
        )
        == 0
    )
    assert len(mailer.sent) == 1
    assert (
        mailer.sent[0][0].get_body(preferencelist=("plain",)).get_content().strip() == item["text"]
    )
    assert calls == [
        "/v1/transports/email/events",
        "/v1/transports/email/outbox/claim",
        "/v1/transports/email/outbox/email-delivery-1/ack",
        "/v1/transports/email/outbox/claim",
    ]


def test_http_intake_failure_keeps_unseen(
    config: email.EmailConfig,
    credentials: email.EmailCredentials,
) -> None:
    inbox = Inbox([("1", mail())])
    transport = httpx.MockTransport(
        lambda _: httpx.Response(503, json={"detail": "secret failure"})
    )
    with ChannelClient(
        "http://local", credentials.service_token, "email", transport=transport
    ) as client:
        assert email.intake_once(config, credentials, client, inbox) == 1
    assert inbox.seen == []
