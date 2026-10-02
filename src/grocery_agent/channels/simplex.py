"""Opt-in transport for an externally provisioned local SimpleX Chat CLI."""

import json
import logging
import os
import re
import time
import tomllib
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from ipaddress import ip_address
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

logger = logging.getLogger(__name__)
ENV_PREFIX = "GROCERY_SIMPLEX_"
MAX_TEXT_BYTES = 8000
MAX_PENDING_EVENTS = 4096
MAX_PENDING_BYTES = 8 * 2**20


def numeric_id(value: Any) -> bool:
    return type(value) is int and 0 < value <= 2**63 - 1


class SimplexSettings(BaseModel):
    """Non-secret configuration. The service credential is environment-only."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    enabled: bool = False
    api_url: str = "http://127.0.0.1:8001"
    ws_url: str = "ws://127.0.0.1:5225"
    user_id: int = Field(default=1, strict=True, gt=0, le=2**63 - 1)
    allowed_contact_ids: tuple[int, ...] = ()
    history_count: int = Field(default=50, strict=True, ge=1, le=200)
    poll_seconds: float = Field(default=1.0, gt=0, le=60, allow_inf_nan=False)
    response_timeout: float = Field(default=15.0, gt=0, le=60, allow_inf_nan=False)
    reconnect_seconds: float = Field(default=1.0, gt=0, le=60, allow_inf_nan=False)
    reconnect_max_seconds: float = Field(default=30.0, gt=0, le=60, allow_inf_nan=False)

    @model_validator(mode="before")
    @classmethod
    def check_contact_ids(cls, values: Any) -> Any:
        if isinstance(values, dict) and "allowed_contact_ids" in values:
            ids = values["allowed_contact_ids"]
            if not isinstance(ids, (list, tuple)) or not all(numeric_id(i) for i in ids):
                raise ValueError("allowed_contact_ids must contain positive numeric contact IDs")
            if len(ids) != len(set(ids)):
                raise ValueError("allowed_contact_ids must be unique")
        return values

    @model_validator(mode="after")
    def check_endpoints(self) -> "SimplexSettings":
        ws = urlsplit(self.ws_url)
        host = ws.hostname or ""
        try:
            loopback = ip_address(host).is_loopback
        except ValueError:
            loopback = host == "localhost"
        if (
            ws.scheme not in {"ws", "wss"}
            or not loopback
            or ws.username is not None
            or ws.password is not None
            or ws.query
            or ws.fragment
        ):
            raise ValueError(
                "SimpleX WebSocket endpoint must be loopback and contain no credentials"
            )
        api = urlsplit(self.api_url)
        if (
            api.scheme not in {"http", "https"}
            or not api.hostname
            or api.username is not None
            or api.password is not None
            or api.query
            or api.fragment
        ):
            raise ValueError("api_url must be an HTTP endpoint without credentials")
        if self.reconnect_max_seconds < self.reconnect_seconds:
            raise ValueError("reconnect_max_seconds must be at least reconnect_seconds")
        return self

    @classmethod
    def load(
        cls, path: Path | None = None, *, environ: Mapping[str, str] | None = None
    ) -> "SimplexSettings":
        values: dict[str, Any] = {}
        if path is not None:
            with path.open("rb") as stream:
                values = tomllib.load(stream)
        env = os.environ if environ is None else environ
        for name in cls.model_fields:
            raw = env.get(ENV_PREFIX + name.upper())
            if raw is not None:
                values[name] = raw if name in {"api_url", "ws_url"} else json.loads(raw)
        return cls.model_validate(values)


class Backend(Protocol):
    def intake(self, event_id: str, address: str, text: str) -> dict[str, Any]: ...
    def claim(self) -> dict[str, Any] | None: ...
    def ack(self, delivery_id: str, lease_token: str, *, error: str | None = None) -> None: ...


class Socket(Protocol):
    def send(self, message: str) -> None: ...
    def recv(self, timeout: float | None = None) -> str | bytes: ...


def connect_websocket(url: str) -> AbstractContextManager[Socket]:
    # Import only after enabled startup. Missing optional dependencies fail clearly.
    try:
        from websockets.sync.client import connect
    except ImportError as exc:
        raise ImportError("Enabled SimpleX requires websockets with its sync API") from exc
    return cast(
        AbstractContextManager[Socket],
        connect(url, open_timeout=10, close_timeout=5, max_size=2**20, proxy=None),
    )


def channel_client(api_url: str, token: str, channel: str) -> AbstractContextManager[Backend]:
    from grocery_agent.http_client import ChannelClient

    return ChannelClient(api_url, token, channel)


def record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def incoming(message: Any, settings: SimplexSettings) -> list[tuple[str, str, str]]:
    """Trust only daemon identity fields, never message text or display names."""
    envelope = record(message)
    if envelope.get("corrId") is not None:
        return []
    resp = record(envelope.get("resp"))
    user_id = record(resp.get("user")).get("userId")
    if resp.get("type") != "newChatItems" or not numeric_id(user_id):
        return []
    if user_id != settings.user_id or not isinstance(resp.get("chatItems"), list):
        return []
    events = []
    for entry in resp["chatItems"]:
        info = record(record(entry).get("chatInfo"))
        item = record(record(entry).get("chatItem"))
        contact = record(info.get("contact")).get("contactId")
        item_id = record(item.get("meta")).get("itemId")
        content = record(item.get("content"))
        msg = record(content.get("msgContent"))
        if (
            info.get("type") == "direct"
            and numeric_id(contact)
            and contact in settings.allowed_contact_ids
            and numeric_id(item_id)
            and record(item.get("chatDir")).get("type") == "directRcv"
            and content.get("type") == "rcvMsgContent"
            and msg.get("type") == "text"
            and isinstance(msg.get("text"), str)
            and item.get("file") is None
            and not record(item.get("meta")).get("itemEdited")
            and not record(item.get("meta")).get("itemLive")
            and record(item.get("meta")).get("itemDeleted") is None
        ):
            events.append((f"simplex:{user_id}:{contact}:{item_id}", str(contact), msg["text"]))
    return events


def send_command(contact_id: int, text: str, corr_id: str) -> str:
    if not numeric_id(contact_id) or not isinstance(text, str):
        raise ValueError("send requires a numeric contact ID and text")
    messages = [{"msgContent": {"type": "text", "text": text}, "mentions": {}}]
    return json.dumps(
        {"corrId": corr_id, "cmd": f"/_send @{contact_id} json {json.dumps(messages)}"}
    )


def bounded_text(text: str) -> str:
    suffix = "\n[Result truncated to fit one SimpleX message.]"
    encoded = text[: MAX_TEXT_BYTES + 1].encode("utf-8")
    if len(encoded) <= MAX_TEXT_BYTES and len(text) <= MAX_TEXT_BYTES:
        return text
    return (
        encoded[: MAX_TEXT_BYTES - len(suffix.encode())].decode("utf-8", errors="ignore") + suffix
    )


def display(value: Any) -> str:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return "?"
    text = str(value)[:601]
    text = re.sub(r"(?:https?|wss?)://\S+", "[link omitted]", text, flags=re.IGNORECASE)
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text).strip()
    return text[:600] + ("…" if len(text) > 600 else "")


def items(value: Any, limit: int) -> list[Any]:
    return value[:limit] if isinstance(value, list) else []


def delivery_text(delivery: dict[str, Any]) -> str:
    """Render RecipeExecutor's report without HTML, offer URLs or bearer links."""
    text = delivery["text"]
    result = record(delivery.get("result"))
    if not result:
        return bounded_text(text)
    lines = [display(text), "Recipe result"]
    if result.get("status") != "ok":
        lines.extend([display(result.get("status")), display(result.get("reason"))])
        return bounded_text("\n".join(lines))
    report = record(result.get("report"))
    lines.append("Meal alternatives; quantities cover all servings, macros/cost are per serving.")
    for meal_value in items(report.get("meals"), 8):
        meal = record(meal_value)
        lines.extend(
            [
                "",
                display(meal.get("title")),
                f"{display(meal.get('servings'))} servings; {display(meal.get('minutes'))} minutes",
            ]
        )
        macros = record(meal.get("nutrients_per_serving"))
        lines.append(
            f"Macros: {display(macros.get('kcal'))} kcal; "
            f"protein {display(macros.get('protein_g'))} g; "
            f"carbs {display(macros.get('carbs_g'))} g; fat {display(macros.get('fat_g'))} g"
        )
        lines.append(f"Usage cost: {display(meal.get('usage_cost_per_serving_czk'))} CZK/serving")
        lines.append("Steps:")
        for number, step in enumerate(items(meal.get("steps"), 16), 1):
            lines.append(f"{number}. {display(step)}")
        lines.append("Shopping / ingredients:")
        for line_value in items(meal.get("lines"), 32):
            line = record(line_value)
            price = record(line.get("price"))
            offer = record(price.get("offer"))
            store = record(offer.get("product")).get("store_id")
            lines.append(
                f"- {display(price.get('label'))}: use {display(line.get('required_grams'))} g, "
                f"owned {display(line.get('owned_grams'))} g, "
                f"buy {display(line.get('purchased_grams'))} g; "
                f"{display(store) if store else 'pantry estimate / already owned'}"
            )
        if len(items(meal.get("steps"), 17)) > 16 or len(items(meal.get("lines"), 33)) > 32:
            lines.append("[Additional steps/ingredients omitted.]")
    if len(items(report.get("meals"), 9)) > 8:
        lines.append("[Additional meal alternatives omitted.]")
    lines.append(
        "Shopping quantities are ingredient mass; package spend may differ from usage cost."
    )
    for warning in items(report.get("warnings"), 6):
        lines.append(f"Warning: {display(warning)}")
    return bounded_text("\n".join(lines))


class SimplexBot:
    def __init__(
        self,
        settings: SimplexSettings,
        backend: Backend,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.backend = backend
        self.clock = clock
        self.pending: dict[str, Any] | None = None
        self.corr_id: str | None = None
        self.deadline = 0.0
        self.inbox: dict[str, tuple[str, str, str]] = {}
        self.inbox_bytes = 0

    def enqueue(self, events: list[tuple[str, str, str]]) -> None:
        for event in events:
            event_id, _, text = event
            if event_id in self.inbox:
                continue
            size = len(text.encode("utf-8"))
            if len(self.inbox) >= MAX_PENDING_EVENTS or self.inbox_bytes + size > MAX_PENDING_BYTES:
                raise RuntimeError("SimpleX pending intake queue capacity exceeded")
            self.inbox[event_id] = event
            self.inbox_bytes += size

    def flush_incoming(self) -> None:
        while self.inbox:
            event_id = next(iter(self.inbox))
            event = self.inbox[event_id]
            result = self.backend.intake(*event)
            if result.get("status") not in {"processed", "duplicate", "ignored"}:
                raise RuntimeError("Backend did not accept SimpleX intake")
            # Remove only after success; ambiguous HTTP failures replay the same ID.
            self.inbox_bytes -= len(event[2].encode("utf-8"))
            del self.inbox[event_id]

    def control(self, socket: Socket, command: str, response_type: str) -> dict[str, Any]:
        corr_id = uuid4().hex
        deadline = self.clock() + self.settings.response_timeout
        socket.send(json.dumps({"corrId": corr_id, "cmd": command}))
        while self.clock() < deadline:
            try:
                raw = socket.recv(timeout=min(self.settings.poll_seconds, deadline - self.clock()))
            except TimeoutError:
                continue
            try:
                message = record(json.loads(raw))
            except (ValueError, UnicodeDecodeError):
                continue
            if message.get("corrId") == corr_id:
                resp = record(message.get("resp"))
                if resp.get("type") != response_type:
                    raise RuntimeError("SimpleX daemon rejected reconciliation command")
                return resp
            # Events can precede the startup profile check; hold them until verified.
            self.enqueue(incoming(message, self.settings))
        raise TimeoutError("SimpleX reconciliation response deadline expired")

    def verify_active(self, socket: Socket) -> None:
        active = self.control(socket, "/user", "activeUser")
        user_id = record(active.get("user")).get("userId")
        if not numeric_id(user_id) or user_id != self.settings.user_id:
            raise RuntimeError("SimpleX daemon active profile differs from configured user_id")

    def reconcile(self, socket: Socket) -> None:
        self.verify_active(socket)
        contacts = self.control(socket, f"/_contacts {self.settings.user_id}", "contactsList")
        contact_user = record(contacts.get("user")).get("userId")
        if not numeric_id(contact_user) or contact_user != self.settings.user_id:
            raise RuntimeError("SimpleX daemon contacts belong to another profile")
        ids = {
            record(contact).get("contactId")
            for contact in items(contacts.get("contacts"), MAX_PENDING_EVENTS)
            if numeric_id(record(contact).get("contactId"))
        }
        if not set(self.settings.allowed_contact_ids) <= ids:
            raise RuntimeError("Configured SimpleX contact is not paired with this profile")
        # Replay failed live events before history can push them out of the window.
        self.flush_incoming()
        for contact in self.settings.allowed_contact_ids:
            resp = self.control(
                socket, f"/_get chat @{contact} count={self.settings.history_count}", "apiChat"
            )
            chat = record(resp.get("chat"))
            info = record(chat.get("chatInfo"))
            history_user = record(resp.get("user")).get("userId")
            history_contact = record(info.get("contact")).get("contactId")
            if (
                not numeric_id(history_user)
                or history_user != self.settings.user_id
                or info.get("type") != "direct"
                or not numeric_id(history_contact)
                or history_contact != contact
                or not isinstance(chat.get("chatItems"), list)
            ):
                raise RuntimeError(
                    "SimpleX history response does not match requested profile/contact"
                )
            recent = chat["chatItems"][-self.settings.history_count :]
            envelope = {
                "resp": {
                    "type": "newChatItems",
                    "user": resp["user"],
                    "chatItems": [{"chatInfo": info, "chatItem": item} for item in recent],
                }
            }
            events = incoming(envelope, self.settings)
            events.sort(key=lambda event: int(event[0].rsplit(":", 1)[1]))
            self.enqueue(events)
            self.flush_incoming()

    def finish(self, error: str | None = None) -> None:
        if self.pending is not None:
            delivery, self.pending = self.pending, None
            self.corr_id = None
            self.backend.ack(delivery["delivery_id"], delivery["lease_token"], error=error)

    def claim(self, socket: Socket) -> None:
        if self.pending is not None:
            return
        delivery = self.backend.claim()
        if delivery is None:
            return
        self.pending = delivery
        addresses = {str(contact): contact for contact in self.settings.allowed_contact_ids}
        contact = addresses.get(delivery["address"])
        if contact is None or not isinstance(delivery.get("text"), str):
            self.finish("SimpleX delivery target is not an allowed contact or text is invalid")
            return
        # /_send uses the active daemon user. Never switch the operator's profile.
        self.verify_active(socket)
        self.flush_incoming()
        self.corr_id = uuid4().hex
        self.deadline = self.clock() + self.settings.response_timeout
        socket.send(send_command(contact, delivery_text(delivery), self.corr_id))

    def handle(self, raw: str | bytes) -> None:
        try:
            message = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return
        envelope = record(message)
        if envelope.get("corrId") is not None:
            if self.pending is not None and envelope["corrId"] == self.corr_id:
                resp = record(envelope.get("resp"))
                if resp.get("type") == "newChatItems":
                    self.finish()
                elif resp.get("type") in {"chatCmdError", "chatError"}:
                    self.finish("SimpleX daemon rejected send")
                # Unknown correlated records do not prove a successful send.
            return
        # Replies belong to the durable backend outbox, not a second direct send.
        self.enqueue(incoming(message, self.settings))
        self.flush_incoming()

    def receive(self, socket: Socket, timeout: float) -> None:
        try:
            raw = socket.recv(timeout=timeout)
        except TimeoutError:
            pass
        else:
            self.handle(raw)
        if self.pending is not None and self.clock() >= self.deadline:
            raise TimeoutError("SimpleX send response deadline expired")

    def session(self, socket: Socket, *, once: bool = False) -> None:
        try:
            if once:
                # One input frame (or timeout), one claim, then resolve that lease.
                self.receive(socket, self.settings.poll_seconds)
                self.claim(socket)
                while self.pending is not None:
                    timeout = min(
                        self.settings.poll_seconds, max(0.001, self.deadline - self.clock())
                    )
                    self.receive(socket, timeout)
                return
            next_poll = self.clock()
            while True:
                now = self.clock()
                if now >= next_poll:
                    self.claim(socket)
                    next_poll = now + self.settings.poll_seconds
                timeout = max(0.001, next_poll - self.clock())
                if self.pending is not None:
                    timeout = min(timeout, max(0.001, self.deadline - self.clock()))
                self.receive(socket, timeout)
        finally:
            # Also release on interrupts. Failed HTTP ack leaves backend lease expiry.
            self.finish("SimpleX connection interrupted; send outcome may be ambiguous")


def run(
    settings: SimplexSettings,
    *,
    once: bool = False,
    environ: Mapping[str, str] | None = None,
    client_factory: Callable[..., AbstractContextManager[Backend]] = channel_client,
    connector: Callable[[str], AbstractContextManager[Socket]] = connect_websocket,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    if not settings.enabled:
        return 0
    env = os.environ if environ is None else environ
    token = env.get(ENV_PREFIX + "TRANSPORT_TOKEN") or env.get(ENV_PREFIX + "SERVICE_TOKEN", "")
    if not token.strip():
        raise ValueError(
            "GROCERY_SIMPLEX_TRANSPORT_TOKEN (or GROCERY_SIMPLEX_SERVICE_TOKEN) is required"
        )
    if not settings.allowed_contact_ids:
        raise ValueError("enabled SimpleX requires locally configured allowed_contact_ids")
    from httpx import HTTPError

    with client_factory(settings.api_url, token, "simplex") as backend:
        bot = SimplexBot(settings, backend)
        delay = settings.reconnect_seconds
        while True:
            try:
                with connector(settings.ws_url) as socket:
                    bot.reconcile(socket)
                    bot.session(socket, once=once)
                return 0
            except (OSError, TimeoutError, RuntimeError, HTTPError) as exc:
                # websockets.ConnectionClosed is not an OSError or RuntimeError.
                logger.warning("SimpleX session interrupted (%s)", type(exc).__name__)
                if once:
                    return 1
            except Exception as exc:
                # Keep the optional dependency lazy even in exception handling.
                if not type(exc).__module__.startswith("websockets."):
                    raise
                logger.warning("SimpleX socket interrupted (%s)", type(exc).__name__)
                if once:
                    return 1
            sleep(delay)
            delay = min(delay * 2, settings.reconnect_max_seconds)
