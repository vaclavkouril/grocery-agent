"""Optional email transport. Identity and MIME checks live here; commands live upstream."""

import hashlib
import imaplib
import json
import os
import re
import smtplib
import ssl
import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, fields
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path
from typing import Any, Protocol, Self

_DOMAIN = r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}"
_ADDRESS = re.compile(
    r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*@" + _DOMAIN
)
_AUTHSERV = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]*")


def valid_address(value: str) -> bool:
    return (
        len(value) <= 254
        and len(value.partition("@")[0]) <= 64
        and _ADDRESS.fullmatch(value) is not None
    )


def normalize_address(value: str) -> str:
    local, separator, domain = value.rpartition("@")
    return local + separator + domain.lower()


@dataclass(frozen=True)
class EmailConfig:
    enabled: bool = False
    intake_enabled: bool = False
    delivery_enabled: bool = False
    api_url: str = "http://127.0.0.1:8001"
    imap_host: str = ""
    imap_port: int = 993
    imap_mailbox: str = "INBOX"
    smtp_host: str = ""
    smtp_port: int = 465
    sender: str = ""
    trusted_authserv_id: str = ""
    trusted_ingress: bool = False
    trusted_senders: tuple[str, ...] = ()
    max_message_bytes: int = 262144
    max_command_chars: int = 4096
    batch_size: int = 20
    poll_seconds: float = 30.0
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        for name in ("enabled", "intake_enabled", "delivery_enabled", "trusted_ingress"):
            if type(getattr(self, name)) is not bool:
                raise ValueError("email switches must be booleans")
        for name in (
            "imap_port",
            "smtp_port",
            "max_message_bytes",
            "max_command_chars",
            "batch_size",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError("email limits must be positive integers")
        if self.imap_port > 65535 or self.smtp_port > 65535:
            raise ValueError("invalid email port")
        for value in (self.poll_seconds, self.timeout_seconds):
            if type(value) not in (int, float) or not 0 < value <= 3600:
                raise ValueError("email timeouts must be between zero and 3600 seconds")
        for name in (
            "api_url",
            "imap_host",
            "imap_mailbox",
            "smtp_host",
            "sender",
            "trusted_authserv_id",
        ):
            if not isinstance(getattr(self, name), str):
                raise ValueError("email configuration strings required")
        object.__setattr__(self, "sender", normalize_address(self.sender))
        if not isinstance(self.trusted_senders, (tuple, list)) or any(
            not isinstance(address, str) or not valid_address(normalize_address(address))
            for address in self.trusted_senders
        ):
            raise ValueError("invalid trusted sender address")
        object.__setattr__(
            self,
            "trusted_senders",
            tuple(normalize_address(address) for address in self.trusted_senders),
        )
        if self.enabled and (self.intake_enabled or self.delivery_enabled):
            if not valid_address(self.sender):
                raise ValueError("email sender must be a single mailbox address")
        if self.enabled and self.intake_enabled:
            if not self.imap_host or not self.imap_mailbox or not self.trusted_senders:
                raise ValueError("IMAP intake requires a host, mailbox and trusted senders")
            if not self.trusted_ingress or not _AUTHSERV.fullmatch(self.trusted_authserv_id):
                raise ValueError("IMAP intake requires a trusted, sanitizing ingress")
        if self.enabled and self.delivery_enabled and not self.smtp_host:
            raise ValueError("SMTP delivery requires a host")

    @classmethod
    def load(cls, path: Path | None = None, *, environ: Mapping[str, str] | None = None) -> Self:
        payload: dict[str, Any] = {}
        if path is not None:
            with path.open("rb") as source:
                payload = tomllib.load(source)
        names = {field.name for field in fields(cls)}
        if payload.keys() - names:
            # Never echo unknown names/values: rejected TOML may contain secrets.
            raise ValueError(
                "unknown email configuration fields (credentials belong in environment)"
            )
        env = os.environ if environ is None else environ
        defaults = cls()
        for name in names:
            key = "GROCERY_EMAIL_" + name.upper()
            if key not in env:
                continue
            raw = env[key]
            default = getattr(defaults, name)
            if isinstance(default, bool):
                if raw.lower() not in {"true", "false", "1", "0"}:
                    raise ValueError("invalid email boolean environment value")
                payload[name] = raw.lower() in {"true", "1"}
            elif isinstance(default, int):
                payload[name] = int(raw)
            elif isinstance(default, float):
                payload[name] = float(raw)
            elif isinstance(default, tuple):
                payload[name] = json.loads(raw)
            else:
                payload[name] = raw
        if "trusted_senders" in payload:
            if not isinstance(payload["trusted_senders"], (list, tuple)):
                raise ValueError("trusted senders must be an array")
            payload["trusted_senders"] = tuple(payload["trusted_senders"])
        return cls(**payload)


@dataclass(frozen=True, repr=False)
class EmailCredentials:
    service_token: str
    imap_username: str = ""
    imap_password: str = ""
    smtp_username: str = ""
    smtp_password: str = ""

    @classmethod
    def load(cls, config: EmailConfig, *, environ: Mapping[str, str] | None = None) -> Self:
        env = os.environ if environ is None else environ
        values = {
            name: env.get("GROCERY_EMAIL_" + name.upper(), "")
            for name in (
                "service_token",
                "imap_username",
                "imap_password",
                "smtp_username",
                "smtp_password",
            )
        }
        required = ["service_token"]
        if config.intake_enabled:
            required += ["imap_username", "imap_password"]
        if config.delivery_enabled:
            required += ["smtp_username", "smtp_password"]
        if any(not values[name] for name in required):
            raise ValueError("missing email service credentials in environment")
        return cls(**values)


class ChannelAPI(Protocol):
    def intake(self, event_id: str, address: str, text: str) -> dict[str, Any]: ...

    def claim(self) -> dict[str, Any] | None: ...

    def ack(self, delivery_id: str, lease_token: str, *, error: str | None = None) -> None: ...


class Inbox(Protocol):
    uidvalidity: str

    def messages(self, limit: int, max_bytes: int) -> Iterator[tuple[str, bytes]]: ...

    def mark_seen(self, uid: str) -> None: ...


class Mailer(Protocol):
    def send(self, message: EmailMessage, sender: str, recipient: str) -> None: ...


def _dmarc_pass(message: EmailMessage, config: EmailConfig, domain: str) -> bool:
    if not config.trusted_ingress:
        return False
    trusted: list[str] = []
    for header in message.get_all("Authentication-Results", []):
        parts = str(header).split(";")
        if parts[0].strip() == config.trusted_authserv_id:
            trusted.append(str(header))
    if len(trusted) != 1:
        return False
    clauses = trusted[0].split(";")[1:]
    dmarc = []
    for clause in clauses:
        # Flat comments are tolerated; nested/unterminated comments and quoted values fail closed.
        clause = re.sub(r"\([^()\r\n]*\)", "", clause).strip()
        if "(" in clause or ")" in clause:
            return False
        if re.match(r"dmarc\s*=", clause, re.IGNORECASE):
            dmarc.append(clause)
    return (
        len(dmarc) == 1
        and re.fullmatch(r"dmarc=pass\s+header\.from=" + re.escape(domain), dmarc[0], re.IGNORECASE)
        is not None
    )


def extract_command(raw: bytes, config: EmailConfig) -> tuple[str, str] | str | None:
    """Return trusted (sender, first command), 'junk', or None (unverifiable)."""
    if len(raw) > config.max_message_bytes:
        return None
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
        if message.defects:
            return None
        from_headers = message.get_all("From", [])
        if len(from_headers) != 1:
            return None
        header = from_headers[0]
        if (
            header.defects
            or len(header.addresses) != 1
            or any(group.display_name is not None for group in header.groups)
        ):
            return None
        address = normalize_address(header.addresses[0].addr_spec)
        if not valid_address(address):
            return None
        # Headerregistry can canonicalize quoted/commented local parts. Require a
        # literal dot-atom mailbox as well, so canonicalization cannot hide ambiguity.
        raw_from = next(value for name, value in message.raw_items() if name.lower() == "from")
        raw_from = re.sub(r"\r?\n[ \t]+", " ", raw_from).strip()
        if "\r" in raw_from or "\n" in raw_from:
            return None
        if "<" in raw_from or ">" in raw_from:
            enclosed = re.fullmatch(r"[^<>]*<([^<>]+)>", raw_from)
            if enclosed is None:
                return None
            raw_from = enclosed[1].strip()
        if normalize_address(raw_from) != address or not valid_address(normalize_address(raw_from)):
            return None
        if address.casefold() == config.sender.casefold():
            return "junk"
        if message.get_all("Auto-Submitted", []) and (
            len(message.get_all("Auto-Submitted", [])) != 1
            or str(message["Auto-Submitted"]).strip().lower() != "no"
        ):
            return "junk"
        if any(name.lower().startswith("list-") for name in message.keys()):
            return "junk"
        if any(
            str(value).strip().lower() in {"bulk", "list", "junk"}
            for value in message.get_all("Precedence", [])
        ):
            return "junk"
        own_id = re.fullmatch(
            r"<grocery-email-[0-9a-f]{64}@" + re.escape(config.sender.rsplit("@", 1)[-1]) + r">",
            str(message.get("Message-ID", "")),
        )
        if own_id:
            return "junk"
        if address not in config.trusted_senders or not _dmarc_pass(
            message, config, address.rsplit("@", 1)[1]
        ):
            return None
        # One inline plain-text body only. Never interpret HTML, attached text, or nested mail.
        plain = []
        for part in message.walk():
            if part.defects or part.get_content_type() == "message/rfc822":
                return None
            if part.is_multipart() and part.get_content_disposition() == "attachment":
                return None
            if (
                part.get_content_type() == "text/plain"
                and part.get_content_disposition() != "attachment"
            ):
                if part.get_filename() is not None:
                    return None
                plain.append(part)
        if len(plain) != 1:
            return None
        text = plain[0].get_content()
        if plain[0].defects or not isinstance(text, str):
            return None
        lines = (line.strip() for line in text.splitlines())
        command = next((line for line in lines if line), "")
        if not command or command.startswith((">", "|")) or len(command) > config.max_command_chars:
            return None
        if any(ord(char) < 32 or ord(char) == 127 for char in command):
            return None
        return address, command
    except (ValueError, LookupError, UnicodeError, AttributeError):
        return None


def event_id(config: EmailConfig, credentials: EmailCredentials, validity: str, uid: str) -> str:
    if not re.fullmatch(r"[1-9][0-9]*", validity) or not re.fullmatch(r"[1-9][0-9]*", uid):
        raise ValueError("invalid IMAP UID identity")
    account = json.dumps(
        [config.imap_host, config.imap_port, credentials.imap_username, config.imap_mailbox],
        separators=(",", ":"),
    )
    digest = hashlib.sha256(account.encode()).hexdigest()
    return f"imap:{digest}:{validity}:{uid}"


def intake_once(
    config: EmailConfig, credentials: EmailCredentials, client: ChannelAPI, inbox: Inbox
) -> int:
    failures = 0
    for uid, raw in inbox.messages(config.batch_size, config.max_message_bytes):
        command = extract_command(raw, config)
        if command is None:
            continue
        try:
            if isinstance(command, tuple):
                address, text = command
                response = client.intake(
                    event_id(config, credentials, inbox.uidvalidity, uid), address, text
                )
                if response.get("status") not in {"processed", "duplicate", "ignored"}:
                    raise ValueError("backend did not accept intake")
            inbox.mark_seen(uid)
        except Exception:
            # Backend and mark failures leave mail for durable upstream replay.
            failures += 1
    return failures


def build_delivery(config: EmailConfig, delivery: dict[str, Any]) -> EmailMessage:
    address = delivery["address"]
    if not isinstance(address, str) or not valid_address(address):
        raise ValueError("invalid backend delivery address")
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = config.sender
    message["To"] = address
    message["Subject"] = delivery["subject"]
    digest = hashlib.sha256(delivery["delivery_id"].encode()).hexdigest()
    message["Message-ID"] = f"<grocery-email-{digest}@{config.sender.rsplit('@', 1)[1]}>"
    message["Auto-Submitted"] = "auto-generated"
    message.set_content(delivery["text"])
    if delivery.get("report_html") is not None:
        message.add_attachment(
            delivery["report_html"].encode("utf-8"),
            maintype="text",
            subtype="html",
            filename="report.html",
        )
    if delivery.get("result") is not None:
        data = json.dumps(delivery["result"], ensure_ascii=False, allow_nan=False).encode("utf-8")
        message.add_attachment(data, maintype="application", subtype="json", filename="result.json")
    return message


def delivery_once(config: EmailConfig, client: ChannelAPI, mailer: Mailer) -> int:
    for _ in range(config.batch_size):
        delivery = client.claim()
        if delivery is None:
            return 0
        try:
            message = build_delivery(config, delivery)
            mailer.send(message, config.sender, delivery["address"])
        except Exception:
            client.ack(
                delivery["delivery_id"], delivery["lease_token"], error="Email delivery failed"
            )
            return 1
        # SMTP acceptance precedes acknowledgment. An ack failure leaves the lease to expire.
        client.ack(delivery["delivery_id"], delivery["lease_token"])
    return 0


class IMAPTransport:
    def __init__(self, config: EmailConfig, credentials: EmailCredentials) -> None:
        self.config = config
        self.credentials = credentials
        self.uidvalidity = ""
        self.connection: imaplib.IMAP4_SSL | None = None

    def __enter__(self) -> Self:
        self.connection = imaplib.IMAP4_SSL(
            self.config.imap_host,
            self.config.imap_port,
            ssl_context=ssl.create_default_context(),
            timeout=self.config.timeout_seconds,
        )
        try:
            self.connection.login(self.credentials.imap_username, self.credentials.imap_password)
            status, _ = self.connection.select(self.config.imap_mailbox)
            if status != "OK":
                raise RuntimeError("IMAP mailbox unavailable")
            _, data = self.connection.response("UIDVALIDITY")
            if not data or not isinstance(data[0], bytes):
                raise RuntimeError("IMAP UIDVALIDITY unavailable")
            self.uidvalidity = data[0].decode("ascii")
            if not re.fullmatch(r"[1-9][0-9]*", self.uidvalidity):
                raise RuntimeError("IMAP UIDVALIDITY invalid")
            return self
        except BaseException:
            self.__exit__()
            raise

    def __exit__(self, *args: Any) -> None:
        if self.connection is not None:
            try:
                self.connection.logout()
            except Exception:
                pass
            self.connection = None

    def messages(self, limit: int, max_bytes: int) -> Iterator[tuple[str, bytes]]:
        assert self.connection is not None
        status, data = self.connection.uid("SEARCH", "UNSEEN")
        if status != "OK" or not data or not isinstance(data[0], bytes):
            raise RuntimeError("IMAP search failed")
        for raw_uid in data[0].split()[:limit]:
            uid = raw_uid.decode("ascii")
            if not re.fullmatch(r"[1-9][0-9]*", uid):
                raise RuntimeError("IMAP UID invalid")
            status, metadata = self.connection.uid("FETCH", uid, "(UID RFC822.SIZE)")
            if status != "OK":
                raise RuntimeError("IMAP size fetch failed")
            rows = [row for row in metadata if isinstance(row, bytes)]
            size = re.search(rb"RFC822.SIZE ([0-9]+)", b" ".join(rows))
            actual_uid = re.search(rb"\bUID ([0-9]+)\b", b" ".join(rows))
            if size is None or actual_uid is None or actual_uid[1] != raw_uid:
                raise RuntimeError("IMAP size identity missing")
            if int(size[1]) > max_bytes:
                continue
            status, body = self.connection.uid("FETCH", uid, "(UID BODY.PEEK[])")
            if status != "OK":
                raise RuntimeError("IMAP body fetch failed")
            literals = [row for row in body if isinstance(row, tuple)]
            if len(literals) != 1:
                raise RuntimeError("IMAP body missing")
            actual_uid = re.search(rb"\bUID ([0-9]+)\b", literals[0][0])
            if actual_uid is None or actual_uid[1] != raw_uid:
                raise RuntimeError("IMAP body identity mismatch")
            raw = literals[0][1]
            if isinstance(raw, bytes) and len(raw) <= max_bytes:
                yield uid, raw

    def mark_seen(self, uid: str) -> None:
        assert self.connection is not None
        status, _ = self.connection.uid("STORE", uid, "+FLAGS.SILENT", "(\\Seen)")
        if status != "OK":
            raise RuntimeError("IMAP seen update failed")


class SMTPTransport:
    def __init__(self, config: EmailConfig, credentials: EmailCredentials) -> None:
        self.config = config
        self.credentials = credentials
        self.connection: smtplib.SMTP_SSL | None = None

    def __enter__(self) -> Self:
        self.connection = smtplib.SMTP_SSL(
            self.config.smtp_host,
            self.config.smtp_port,
            context=ssl.create_default_context(),
            timeout=self.config.timeout_seconds,
        )
        try:
            self.connection.login(self.credentials.smtp_username, self.credentials.smtp_password)
            return self
        except BaseException:
            self.__exit__()
            raise

    def __exit__(self, *args: Any) -> None:
        if self.connection is not None:
            try:
                self.connection.quit()
            except Exception:
                self.connection.close()
            self.connection = None

    def send(self, message: EmailMessage, sender: str, recipient: str) -> None:
        assert self.connection is not None
        refused = self.connection.send_message(message, from_addr=sender, to_addrs=[recipient])
        if refused:
            raise RuntimeError("SMTP recipient not accepted")
