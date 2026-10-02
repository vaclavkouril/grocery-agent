"""Offline SimpleX daemon frames and injectable frozen backend contract."""

import json
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

import httpx
import pytest

from grocery_agent.apps import simplex as app
from grocery_agent.channels.simplex import (
    MAX_TEXT_BYTES,
    SimplexBot,
    SimplexSettings,
    connect_websocket,
    delivery_text,
    incoming,
    run,
    send_command,
)
from tests.test_backend_recipes import executor_inputs as executor_inputs

RECIPE = "recipe provider=template servings=2 have=rice=500g budget=80"


def event(text="help", *, user=1, contact=2, item_id=17):
    return {
        "resp": {
            "type": "newChatItems",
            "user": {"userId": user},
            "chatItems": [
                {
                    "chatInfo": {"type": "direct", "contact": {"contactId": contact}},
                    "chatItem": {
                        "chatDir": {"type": "directRcv"},
                        "meta": {"itemId": item_id},
                        "content": {
                            "type": "rcvMsgContent",
                            "msgContent": {
                                "type": "text",
                                "text": text,
                            },
                        },
                    },
                }
            ],
        },
    }


def delivery(address="2", text="confirm TOKEN"):
    return dict(
        delivery_id="delivery-1",
        lease_token="lease-1",
        address=address,
        text=text,
        report_html=None,
        result=None,
        subject="Recipe",
    )


class BackendFixture:
    def __init__(self, outbox=()):
        self.outbox = list(outbox)
        self.intakes = []
        self.acks = []
        self.claims = 0
        self.seen = set()

    def intake(self, event_id, address, text):
        self.intakes.append((event_id, address, text))
        if event_id in self.seen:
            return {"status": "duplicate", "reply": None}
        self.seen.add(event_id)
        return {"status": "processed", "reply": "confirm TOKEN"}

    def claim(self):
        self.claims += 1
        return self.outbox.pop(0) if self.outbox else None

    def ack(self, delivery_id, lease_token, *, error=None):
        self.acks.append((delivery_id, lease_token, error))


class SocketFixture:
    def __init__(self, frames=(), *, on_send=None):
        self.frames = list(frames)
        self.sent = []
        self.on_send = on_send
        self.now = 0.0
        self.controls = []
        self.user_id = 1
        self.contact_ids = [2]
        self.history = {}

    def send(self, message):
        command = json.loads(message)
        cmd = command["cmd"]
        if not cmd.startswith("/_send "):
            self.controls.append(command)
            if cmd == "/user":
                resp = {"type": "activeUser", "user": {"userId": self.user_id}}
            elif cmd.startswith("/_contacts "):
                resp = {
                    "type": "contactsList",
                    "user": {"userId": self.user_id},
                    "contacts": [{"contactId": c} for c in self.contact_ids],
                }
            elif cmd.startswith("/_get chat @"):
                contact = int(cmd.split("@")[1].split()[0])
                resp = {
                    "type": "apiChat",
                    "user": {"userId": self.user_id},
                    "chat": {
                        "chatInfo": {"type": "direct", "contact": {"contactId": contact}},
                        "chatItems": self.history.get(contact, []),
                    },
                }
            else:
                pytest.fail("unexpected daemon control command")
            self.frames.append({"corrId": command["corrId"], "resp": resp})
            return
        self.sent.append(message)
        if self.on_send:
            self.on_send(self, json.loads(message))

    def recv(self, timeout=None):
        assert timeout > 0
        if self.frames:
            frame = self.frames.pop(0)
            if isinstance(frame, BaseException):
                raise frame
            return frame if isinstance(frame, (str, bytes)) else json.dumps(frame)
        self.now += timeout
        raise TimeoutError


def accepted(socket, command):
    response = event(command["cmd"], item_id=99)
    item = response["resp"]["chatItems"][0]["chatItem"]
    item["chatDir"]["type"] = "directSnd"
    item["content"]["type"] = "sndMsgContent"
    response["corrId"] = command["corrId"]
    socket.frames.append(response)


def settings(**kwargs):
    return SimplexSettings(allowed_contact_ids=(2,), **kwargs)


def test_disabled_never_opens_clients_or_requires_credential():
    def forbidden(*args):
        pytest.fail("disabled startup opened a client")

    assert run(SimplexSettings(), client_factory=forbidden, connector=forbidden, environ={}) == 0


def test_disabled_application_entrypoint():
    path = Path(__file__).resolve().parents[1] / "config/simplex.toml"
    assert app.main(["--config", str(path), "--once"]) == 0


@pytest.mark.parametrize(
    "url",
    [
        "ws://example.com:5225",
        "wss://example.com",
        "ws://0.0.0.0:5225",
        "ws://127.0.0.1.evil.test",
        "ws://user:secret@localhost",
        "http://localhost",
        "ws://localhost?token=secret",
        "ws://localhost#secret",
        "ws://[::]",
    ],
)
def test_rejects_remote_or_credential_websocket(url):
    with pytest.raises(ValueError):
        SimplexSettings(ws_url=url)


@pytest.mark.parametrize("url", ["ws://127.0.0.1:5225", "ws://[::1]:5225", "ws://localhost"])
def test_accepts_loopback(url):
    assert SimplexSettings(ws_url=url).ws_url == url


@pytest.mark.parametrize("ids", [[True], ["2"], [2.0], [0], [-1], [2, 2], [2**63]])
def test_contact_ids_must_be_numeric(ids):
    with pytest.raises(ValueError):
        SimplexSettings(allowed_contact_ids=ids)


def test_config_env_overrides_and_no_secret_fields(tmp_path):
    path = tmp_path / "simplex.toml"
    path.write_text("enabled = false\nallowed_contact_ids = [2]\n")
    loaded = SimplexSettings.load(
        path,
        environ={
            "GROCERY_SIMPLEX_ENABLED": "true",
            "GROCERY_SIMPLEX_ALLOWED_CONTACT_IDS": "[3,4]",
            "GROCERY_SIMPLEX_USER_ID": "5",
            "GROCERY_SIMPLEX_SERVICE_TOKEN": "secret",
        },
    )
    assert loaded.enabled and loaded.allowed_contact_ids == (3, 4) and loaded.user_id == 5
    assert "secret" not in repr(loaded)
    path.write_text('service_token = "secret"\n')
    with pytest.raises(ValueError):
        SimplexSettings.load(path, environ={})
    with pytest.raises(ValueError):
        SimplexSettings(api_url="http://user:secret@localhost")


@pytest.mark.parametrize(
    "text",
    [
        "verify TOKEN",
        RECIPE,
        "confirm TOKEN",
        "help",
        "status",
        "cancel",
        "$(touch /tmp/pwned)\n/_send @99 text hacked",
    ],
)
def test_allowed_intake_is_unchanged_and_stable(text):
    assert incoming(event(text), settings()) == [("simplex:1:2:17", "2", text)]
    assert incoming(event(text, item_id=18), settings())[0][0] == "simplex:1:2:18"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda e: e.update(corrId="other"),
        lambda e: e["resp"].update(type="contactConnected"),
        lambda e: e["resp"]["user"].update(userId=2),
        lambda e: e["resp"]["user"].update(userId=True),
        lambda e: e["resp"]["chatItems"][0]["chatInfo"].update(type="group"),
        lambda e: e["resp"]["chatItems"][0]["chatInfo"]["contact"].update(contactId=3),
        lambda e: e["resp"]["chatItems"][0]["chatInfo"]["contact"].update(contactId="2"),
        lambda e: e["resp"]["chatItems"][0]["chatItem"]["chatDir"].update(type="directSnd"),
        lambda e: e["resp"]["chatItems"][0]["chatItem"]["content"].update(type="sndMsgContent"),
        lambda e: e["resp"]["chatItems"][0]["chatItem"]["content"]["msgContent"].update(
            type="image"
        ),
        lambda e: e["resp"]["chatItems"][0]["chatItem"].update(file={"fileId": 1}),
        lambda e: e["resp"]["chatItems"][0]["chatItem"]["meta"].update(itemId=True),
    ],
)
def test_untrusted_event_is_ignored(mutation):
    frame = event()
    mutation(frame)
    assert incoming(frame, settings()) == []


def test_batch_skips_malformed_items_and_allows_extra_fields():
    frame = event()
    frame["resp"]["chatItems"].extend(
        [None, "unknown", {}, deepcopy(frame["resp"]["chatItems"][0])]
    )
    frame["resp"]["future"] = True
    assert len(incoming(frame, settings())) == 2


def test_backend_owns_dedup_and_replies():
    backend = BackendFixture()
    bot = SimplexBot(settings(), backend)
    bot.handle(json.dumps(event(RECIPE)))
    bot.handle(json.dumps(event(RECIPE)))
    assert backend.intakes == [("simplex:1:2:17", "2", RECIPE)] * 2
    assert bot.pending is None and not backend.acks


def test_send_text_is_json_data():
    text = '"\\\n/_send @99 text wrong\n$(whoami) česky'
    command = json.loads(send_command(2, text, "correlation"))
    assert command["corrId"] == "correlation"
    prefix = "/_send @2 json "
    assert command["cmd"].startswith(prefix)
    composed = json.loads(command["cmd"][len(prefix) :])
    assert composed == [{"msgContent": {"type": "text", "text": text}, "mentions": {}}]
    with pytest.raises(ValueError):
        send_command("2\n/_newcontact", "text", "id")


def test_once_waits_for_correct_response_and_intakes_interleaved_events():
    backend = BackendFixture([delivery(), delivery()])

    def on_send(socket, command):
        assert not backend.acks
        unrelated = event("do not intake response")
        unrelated["corrId"] = "unrelated"
        socket.frames.extend(
            [
                unrelated,
                event("verify TOKEN", item_id=18),
                {"corrId": command["corrId"], "resp": {"type": "futureResponse"}},
            ]
        )
        accepted(socket, command)

    socket = SocketFixture([event(RECIPE)], on_send=on_send)
    SimplexBot(settings(), backend, clock=lambda: socket.now).session(socket, once=True)
    assert backend.claims == 1 and len(socket.sent) == 1
    assert [x[2] for x in backend.intakes] == [RECIPE, "verify TOKEN"]
    assert backend.acks == [("delivery-1", "lease-1", None)]


@pytest.mark.parametrize("frames", [["bad json", {"resp": {"type": "future"}}], []])
def test_unknown_frames_and_receive_timeout(frames):
    backend = BackendFixture()
    socket = SocketFixture(frames)
    SimplexBot(settings(), backend, clock=lambda: socket.now).session(socket, once=True)
    assert backend.claims == 1 and not backend.intakes and not socket.sent


@pytest.mark.parametrize("address", ["99", "simplex:1:2", "@2", "2;whoami", "02"])
def test_outbox_target_must_be_locally_allowed(address):
    backend = BackendFixture([delivery(address)])
    socket = SocketFixture()
    SimplexBot(settings(), backend, clock=lambda: socket.now).session(socket, once=True)
    assert not socket.sent and backend.acks[0][2] is not None


def test_command_error_nacks_without_leaking_daemon_details():
    backend = BackendFixture([delivery()])

    def rejected(socket, command):
        socket.frames.append(
            {
                "corrId": command["corrId"],
                "resp": {
                    "type": "chatCmdError",
                    "chatError": "sensitive details",
                },
            }
        )

    socket = SocketFixture(on_send=rejected)
    SimplexBot(settings(), backend, clock=lambda: socket.now).session(socket, once=True)
    assert backend.acks == [("delivery-1", "lease-1", "SimpleX daemon rejected send")]


@pytest.mark.parametrize("failure", [ConnectionError("disconnected"), KeyboardInterrupt()])
def test_disconnection_or_interrupt_releases_lease(failure):
    backend = BackendFixture([delivery()])
    socket = SocketFixture(on_send=lambda s, c: s.frames.append(failure))
    with pytest.raises(type(failure)):
        SimplexBot(settings(), backend, clock=lambda: socket.now).session(socket, once=True)
    assert len(backend.acks) == 1 and "ambiguous" in backend.acks[0][2]


def test_send_failure_releases_lease():
    backend = BackendFixture([delivery()])

    def broken(socket, command):
        raise ConnectionError("send disconnected")

    socket = SocketFixture(on_send=broken)
    with pytest.raises(ConnectionError):
        SimplexBot(settings(), backend, clock=lambda: socket.now).session(socket, once=True)
    assert len(backend.acks) == 1 and backend.acks[0][2]


def test_unknown_response_expires_lease():
    backend = BackendFixture([delivery()])
    socket = SocketFixture()
    with pytest.raises(TimeoutError):
        SimplexBot(settings(response_timeout=2), backend, clock=lambda: socket.now).session(
            socket,
            once=True,
        )
    assert socket.now == 3  # initial receive timeout plus send response deadline
    assert len(backend.acks) == 1 and backend.acks[0][2]


def test_poll_outbox_on_receive_timeouts():
    backend = BackendFixture()

    class PollSocket(SocketFixture):
        def recv(self, timeout=None):
            if backend.claims >= 3:
                raise KeyboardInterrupt
            return super().recv(timeout)

    socket = PollSocket()
    with pytest.raises(KeyboardInterrupt):
        SimplexBot(settings(), backend, clock=lambda: socket.now).session(socket)
    assert backend.claims == 3 and socket.now == 2


def test_reconnect_has_bounded_backoff_and_uses_service_identity():
    backend = BackendFixture([delivery()])
    calls, delays = [], []

    @contextmanager
    def client(api_url, token, channel):
        calls.append((api_url, token, channel))
        yield backend

    @contextmanager
    def disconnected(url):
        socket = SocketFixture([ConnectionError("offline")])
        yield socket

    def sleep(delay):
        delays.append(delay)
        if len(delays) == 5:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run(
            settings(enabled=True, reconnect_max_seconds=4),
            environ={
                "GROCERY_SIMPLEX_SERVICE_TOKEN": "service-token",
            },
            client_factory=client,
            connector=disconnected,
            sleep=sleep,
        )
    assert delays == [1, 2, 4, 4, 4]
    assert calls == [("http://127.0.0.1:8001", "service-token", "simplex")]
    assert not backend.acks and not backend.claims


def test_once_connection_failure_does_not_reconnect():
    @contextmanager
    def client(*args):
        yield BackendFixture()

    @contextmanager
    def disconnected(url):
        raise ConnectionError("offline")
        yield

    assert (
        run(
            settings(enabled=True),
            once=True,
            environ={
                "GROCERY_SIMPLEX_SERVICE_TOKEN": "service",
            },
            client_factory=client,
            connector=disconnected,
            sleep=lambda _: pytest.fail("--once must not reconnect"),
        )
        == 1
    )


def test_enabled_requires_service_token_and_configured_contacts():
    with pytest.raises(ValueError, match="SERVICE_TOKEN"):
        run(settings(enabled=True), environ={})
    with pytest.raises(ValueError, match="allowed_contact_ids"):
        run(SimplexSettings(enabled=True), environ={"GROCERY_SIMPLEX_SERVICE_TOKEN": "service"})


def test_real_channel_client_with_offline_http_transport():
    from grocery_agent.http_client import ChannelClient

    requests = []

    def handler(request):
        assert request.method == "POST"
        assert request.headers["Authorization"] == "Bearer simplex-service"
        body = json.loads(request.content) if request.content else None
        requests.append((request.url.path, body))
        if request.url.path.endswith("/events"):
            assert body == {"event_id": "simplex:1:2:17", "address": "2", "text": RECIPE}
            return httpx.Response(200, json={"status": "processed", "reply": "DO NOT SEND THIS"})
        if request.url.path.endswith("/claim"):
            return httpx.Response(200, json=delivery(text="confirm TOKEN from durable outbox"))
        assert request.url.path.endswith("/delivery-1/ack")
        assert body == {"lease_token": "lease-1", "error": None}
        return httpx.Response(204)

    def client(api_url, token, channel):
        return ChannelClient(api_url, token, channel, transport=httpx.MockTransport(handler))

    socket = SocketFixture([event(RECIPE)], on_send=accepted)

    @contextmanager
    def connector(url):
        yield socket

    assert (
        run(
            settings(enabled=True),
            once=True,
            environ={
                "GROCERY_SIMPLEX_SERVICE_TOKEN": "simplex-service",
            },
            client_factory=client,
            connector=connector,
        )
        == 0
    )
    assert [path for path, _ in requests] == [
        "/v1/transports/simplex/events",
        "/v1/transports/simplex/outbox/claim",
        "/v1/transports/simplex/outbox/delivery-1/ack",
    ]
    assert len(socket.sent) == 1 and "DO NOT SEND THIS" not in socket.sent[0]
    assert "confirm TOKEN from durable outbox" in socket.sent[0]


def test_event_id_is_bounded_and_namespaces_user_contact_and_item():
    limit = 2**63 - 1
    frame = event(user=limit, contact=limit, item_id=limit)
    configured = SimplexSettings(user_id=limit, allowed_contact_ids=(limit,))
    event_id, address, _ = incoming(frame, configured)[0]
    assert len(event_id) <= 200 and address == str(limit)
    assert event_id == f"simplex:{limit}:{limit}:{limit}"


def test_ack_failure_does_not_send_or_ack_again():
    class FailingBackend(BackendFixture):
        def ack(self, delivery_id, lease_token, *, error=None):
            super().ack(delivery_id, lease_token, error=error)
            raise httpx.ConnectError("backend disconnected")

    backend = FailingBackend([delivery()])
    socket = SocketFixture(on_send=accepted)
    with pytest.raises(httpx.ConnectError):
        SimplexBot(settings(), backend, clock=lambda: socket.now).session(socket, once=True)
    assert len(backend.acks) == 1 and len(socket.sent) == 1


def test_reconnect_reclaims_backend_delivery_after_ambiguous_disconnect():
    class RetryBackend(BackendFixture):
        def ack(self, delivery_id, lease_token, *, error=None):
            super().ack(delivery_id, lease_token, error=error)
            if error:
                retried = delivery()
                retried["lease_token"] = "lease-2"
                self.outbox.append(retried)

    backend = RetryBackend([delivery()])
    sockets, delays = [], []

    @contextmanager
    def client(*args):
        yield backend

    class RetrySocket(SocketFixture):
        def recv(self, timeout=None):
            if not self.frames:
                raise KeyboardInterrupt
            return super().recv(timeout)

    @contextmanager
    def connector(url):
        if not sockets:
            socket = RetrySocket(on_send=lambda s, c: s.frames.append(ConnectionError("lost")))
        else:
            socket = RetrySocket(on_send=accepted)
        sockets.append(socket)
        yield socket

    with pytest.raises(KeyboardInterrupt):
        run(
            settings(enabled=True),
            environ={"GROCERY_SIMPLEX_SERVICE_TOKEN": "service"},
            client_factory=client,
            connector=connector,
            sleep=delays.append,
        )
    assert delays == [1]
    assert len(sockets) == 2 and all(len(s.sent) == 1 for s in sockets)
    assert backend.acks[0][:2] == ("delivery-1", "lease-1") and backend.acks[0][2]
    assert backend.acks[1] == ("delivery-1", "lease-2", None)
    assert json.loads(sockets[0].sent[0])["corrId"] != json.loads(sockets[1].sent[0])["corrId"]


def test_timeout_while_pending_never_acknowledges_success():
    backend = BackendFixture([delivery()])
    socket = SocketFixture()
    bot = SimplexBot(settings(response_timeout=5), backend, clock=lambda: socket.now)
    bot.claim(socket)
    bot.receive(socket, 1)
    assert bot.pending is not None and not backend.acks
    accepted(socket, json.loads(socket.sent[0]))
    bot.receive(socket, 1)
    assert bot.pending is None and backend.acks == [("delivery-1", "lease-1", None)]


def sent_text(socket):
    command = json.loads(socket.sent[0])["cmd"]
    composed = json.loads(command.split(" json ", 1)[1])
    return composed[0]["msgContent"]["text"]


def test_completion_sends_actual_backend_recipe_report(executor_inputs):
    executor, request, inputs = executor_inputs
    request = request.model_copy(update={"provider": "template"})
    result, html = executor.execute(request, inputs)
    assert result["status"] == "ok"
    completed = delivery(text="Job recipe-1: succeeded.")
    completed.update(result=result, report_html=html)
    backend = BackendFixture([completed])
    socket = SocketFixture(on_send=accepted)
    SimplexBot(settings(), backend, clock=lambda: socket.now).session(socket, once=True)
    text = sent_text(socket)
    meal = result["report"]["meals"][0]
    assert meal["title"] in text and meal["steps"][0] in text
    assert f"protein {meal['nutrients_per_serving']['protein_g']} g" in text
    assert f"{meal['nutrients_per_serving']['kcal']} kcal" in text
    assert f"{meal['usage_cost_per_serving_czk']} CZK/serving" in text
    ingredient = meal["lines"][0]
    assert ingredient["price"]["label"] in text
    assert f"buy {ingredient['purchased_grams']} g" in text
    assert "billa" in text and "Steps:" in text and "Shopping / ingredients:" in text
    assert "http://" not in text and "https://" not in text and "<html" not in text
    assert len(text.encode("utf-8")) <= MAX_TEXT_BYTES
    assert backend.acks == [("delivery-1", "lease-1", None)]


def test_result_text_bounded_unicode_and_explicit_truncation():
    meal = {
        "title": "Recipe",
        "steps": ["🥕" * 10000] * 1000,
        "lines": [{"price": {"label": "rice"}, "purchased_grams": "300"}] * 1000,
    }
    completed = delivery()
    completed["result"] = {"status": "ok", "report": {"meals": [meal] * 1000}}
    text = delivery_text(completed)
    assert len(text.encode("utf-8")) <= MAX_TEXT_BYTES
    assert text.endswith("[Result truncated to fit one SimpleX message.]")
    assert "Steps:" in text and "Macros:" in text


def test_result_links_and_unrecognized_fields_are_not_forwarded():
    completed = delivery(text="Job succeeded https://backend/report?bearer=private")
    completed["report_html"] = "<h1>private HTML</h1>"
    completed["result"] = {
        "status": "ok",
        "private_url": "https://backend?token=secret",
        "report": {
            "meals": [
                {
                    "title": "Rice https://backend?token=private",
                    "steps": ["Cook rice", "Visit https://backend?bearer=private"],
                    "lines": [
                        {
                            "price": {
                                "label": "rice",
                                "offer": {
                                    "source_url": "https://store?token=secret",
                                    "product": {"store_id": "billa"},
                                },
                            }
                        }
                    ],
                }
            ]
        },
    }
    text = delivery_text(completed)
    assert "Cook rice" in text and "billa" in text and "[link omitted]" in text
    assert "http" not in text and "private" not in text and "secret" not in text


def test_no_feasible_result_includes_backend_reason():
    completed = delivery(text="Job succeeded.")
    completed["result"] = {
        "status": "no-feasible-recipe",
        "reason": "No configured template remains after exclusions.",
    }
    assert "no-feasible-recipe" in delivery_text(completed)
    assert "No configured template remains after exclusions." in delivery_text(completed)


def test_validation_and_startup_errors_do_not_expose_inputs(tmp_path, capsys):
    sentinel = "sensitive-credential-value"
    with pytest.raises(ValueError) as error:
        SimplexSettings(service_token=sentinel)
    assert sentinel not in str(error.value)
    config = tmp_path / "secret.toml"
    config.write_text(f'service_token = "{sentinel}"\n')
    with pytest.raises(SystemExit) as exited:
        app.main(["--config", str(config)])
    assert exited.value.code == 2
    captured = capsys.readouterr()
    assert sentinel not in captured.err and sentinel not in captured.out
    assert "SimpleX startup failed" in captured.err


def test_runtime_failure_logs_only_exception_class(caplog):
    @contextmanager
    def client(*args):
        yield BackendFixture()

    @contextmanager
    def connector(url):
        raise ConnectionError("Bearer secret-token and verify private-challenge")
        yield

    assert (
        run(
            settings(enabled=True),
            once=True,
            environ={"GROCERY_SIMPLEX_SERVICE_TOKEN": "secret-token"},
            client_factory=client,
            connector=connector,
        )
        == 1
    )
    assert "ConnectionError" in caplog.text
    assert "secret-token" not in caplog.text and "private-challenge" not in caplog.text


def test_compose_transport_token_takes_precedence_over_alias():
    @contextmanager
    def client(url, token, channel):
        assert token == "compose-service-token" and channel == "simplex"
        yield BackendFixture()

    @contextmanager
    def connector(url):
        yield SocketFixture([event()])

    assert (
        run(
            settings(enabled=True),
            once=True,
            environ={
                "GROCERY_SIMPLEX_TRANSPORT_TOKEN": "compose-service-token",
                "GROCERY_SIMPLEX_SERVICE_TOKEN": "older-alias-value",
            },
            client_factory=client,
            connector=connector,
        )
        == 0
    )


def test_lazy_sync_connector_disables_environment_proxy(monkeypatch):
    import websockets.sync.client

    calls = []

    @contextmanager
    def connect(url, **kwargs):
        calls.append((url, kwargs))
        yield SocketFixture()

    monkeypatch.setattr(websockets.sync.client, "connect", connect)
    with connect_websocket("ws://127.0.0.1:5225") as socket:
        assert isinstance(socket, SocketFixture)
    assert calls == [
        (
            "ws://127.0.0.1:5225",
            {
                "open_timeout": 10,
                "close_timeout": 5,
                "max_size": 2**20,
                "proxy": None,
            },
        )
    ]


def history_item(text="help", item_id=17):
    return event(text, item_id=item_id)["resp"]["chatItems"][0]["chatItem"]


def test_initial_profile_mismatch_refuses_intake_history_and_send():
    backend = BackendFixture([delivery()])
    socket = SocketFixture([event("verify TOKEN")])
    socket.user_id = 2
    bot = SimplexBot(settings(), backend, clock=lambda: socket.now)
    with pytest.raises(RuntimeError, match="active profile"):
        bot.reconcile(socket)
    assert [c["cmd"] for c in socket.controls] == ["/user"]
    assert not socket.sent and not backend.intakes and not backend.claims


def test_changed_active_profile_refuses_private_outbox_delivery():
    backend = BackendFixture([delivery(text="private recipe")])
    socket = SocketFixture(on_send=accepted)
    bot = SimplexBot(settings(), backend, clock=lambda: socket.now)
    bot.reconcile(socket)
    socket.user_id = 2
    with pytest.raises(RuntimeError, match="active profile"):
        bot.session(socket, once=True)
    assert not socket.sent
    assert [c["cmd"] for c in socket.controls] == [
        "/user",
        "/_contacts 1",
        "/_get chat @2 count=50",
        "/user",
    ]
    assert len(backend.acks) == 1 and backend.acks[0][2] is not None


def test_reconciliation_refuses_missing_paired_contact():
    backend = BackendFixture()
    socket = SocketFixture([event()])
    socket.contact_ids = [3]
    with pytest.raises(RuntimeError, match="not paired"):
        SimplexBot(settings(), backend, clock=lambda: socket.now).reconcile(socket)
    assert not backend.intakes
    assert [c["cmd"] for c in socket.controls] == ["/user", "/_contacts 1"]


def test_history_replays_only_received_immutable_text_oldest_first_and_backend_dedups():
    backend = BackendFixture()
    socket = SocketFixture()
    outgoing = history_item("outgoing", 20)
    outgoing["chatDir"]["type"] = "directSnd"
    outgoing["content"]["type"] = "sndMsgContent"
    edited = history_item("edited", 21)
    edited["meta"]["itemEdited"] = True
    deleted = history_item("deleted", 22)
    deleted["meta"]["itemDeleted"] = {"type": "deleted"}
    live = history_item("live", 23)
    live["meta"]["itemLive"] = True
    socket.history[2] = [
        history_item("status", 18),
        outgoing,
        edited,
        deleted,
        live,
        history_item("help", 17),
    ]
    bot = SimplexBot(settings(), backend, clock=lambda: socket.now)
    bot.reconcile(socket)
    assert backend.intakes == [("simplex:1:2:17", "2", "help"), ("simplex:1:2:18", "2", "status")]
    bot.handle(json.dumps(event("help")))
    bot.reconcile(socket)
    assert backend.seen == {"simplex:1:2:17", "simplex:1:2:18"}
    assert len(backend.intakes) == 5 and not backend.acks
    assert not socket.sent and not bot.inbox


def test_history_window_is_bounded_and_command_is_from_config_only():
    backend = BackendFixture()
    socket = SocketFixture()
    socket.history[2] = [history_item(item_id=i) for i in range(1, 6)]
    bot = SimplexBot(settings(history_count=2), backend, clock=lambda: socket.now)
    bot.reconcile(socket)
    assert socket.controls[-1]["cmd"] == "/_get chat @2 count=2"
    assert [e[0] for e in backend.intakes] == ["simplex:1:2:4", "simplex:1:2:5"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r["user"].update(userId=2),
        lambda r: r["chat"]["chatInfo"].update(type="group"),
        lambda r: r["chat"]["chatInfo"]["contact"].update(contactId=3),
    ],
)
def test_history_response_must_match_profile_and_requested_contact(mutation):
    class WrongHistory(SocketFixture):
        def send(self, message):
            super().send(message)
            if json.loads(message)["cmd"].startswith("/_get chat"):
                mutation(self.frames[-1]["resp"])

    socket = WrongHistory()
    socket.history[2] = [history_item()]
    backend = BackendFixture()
    with pytest.raises(RuntimeError, match="history response"):
        SimplexBot(settings(), backend, clock=lambda: socket.now).reconcile(socket)
    assert not backend.intakes and not backend.acks


def test_failed_batch_intake_retains_entire_batch_and_retries_after_profile_check():
    class AmbiguousBackend(BackendFixture):
        def intake(self, *args):
            result = super().intake(*args)
            if len(self.intakes) == 1:
                raise httpx.ConnectError("HTTP response lost")
            return result

    backend = AmbiguousBackend()
    socket = SocketFixture()
    bot = SimplexBot(settings(), backend, clock=lambda: socket.now)
    batch = event("help")
    batch["resp"]["chatItems"].extend(event("status", item_id=18)["resp"]["chatItems"])
    with pytest.raises(httpx.ConnectError):
        bot.handle(json.dumps(batch))
    assert list(bot.inbox) == ["simplex:1:2:17", "simplex:1:2:18"]
    socket.user_id = 2
    with pytest.raises(RuntimeError):
        bot.reconcile(socket)
    assert len(backend.intakes) == 1  # wrong profile cannot flush private commands
    socket.user_id = 1
    bot.reconcile(socket)
    assert [e[0] for e in backend.intakes] == ["simplex:1:2:17", "simplex:1:2:17", "simplex:1:2:18"]
    assert not bot.inbox and bot.inbox_bytes == 0


def test_run_keeps_failed_intake_queue_across_reconnect_and_reconciles_history():
    class AmbiguousBackend(BackendFixture):
        def intake(self, *args):
            result = super().intake(*args)
            if len(self.intakes) == 1:
                raise httpx.ConnectError("response lost")
            return result

    class EndingSocket(SocketFixture):
        def recv(self, timeout=None):
            if not self.frames:
                raise KeyboardInterrupt
            return super().recv(timeout)

    backend = AmbiguousBackend()
    sockets, delays = [], []

    @contextmanager
    def client(*args):
        yield backend

    @contextmanager
    def connector(url):
        socket = EndingSocket([event("help")] if not sockets else [])
        socket.history[2] = [history_item("help"), history_item("status", 18)]
        sockets.append(socket)
        yield socket

    with pytest.raises(KeyboardInterrupt):
        run(
            settings(enabled=True),
            environ={"GROCERY_SIMPLEX_TRANSPORT_TOKEN": "service"},
            client_factory=client,
            connector=connector,
            sleep=delays.append,
        )
    assert len(sockets) == 2 and delays == [1]
    assert [e[0] for e in backend.intakes] == [
        "simplex:1:2:17",
        "simplex:1:2:17",
        "simplex:1:2:17",
        "simplex:1:2:18",
    ]
    assert backend.seen == {"simplex:1:2:17", "simplex:1:2:18"}


def test_reconciliation_timeout_is_bounded_without_ack():
    class SilentSocket(SocketFixture):
        def send(self, message):
            self.controls.append(json.loads(message))

    socket = SilentSocket()
    backend = BackendFixture()
    with pytest.raises(TimeoutError):
        SimplexBot(settings(response_timeout=2), backend, clock=lambda: socket.now).reconcile(
            socket
        )
    assert socket.now == 2 and not backend.acks


def test_backend_timeout_propagates_and_keeps_received_event():
    class TimedOutBackend(BackendFixture):
        def intake(self, *args):
            raise TimeoutError("backend stalled")

    backend = TimedOutBackend()
    socket = SocketFixture([event()])
    bot = SimplexBot(settings(), backend, clock=lambda: socket.now)
    with pytest.raises(TimeoutError, match="backend stalled"):
        bot.receive(socket, 1)
    assert list(bot.inbox) == ["simplex:1:2:17"]


def test_queue_capacity_retains_existing_event_without_silent_eviction(monkeypatch):
    from grocery_agent.channels import simplex

    monkeypatch.setattr(simplex, "MAX_PENDING_EVENTS", 1)
    bot = SimplexBot(settings(), BackendFixture())
    bot.enqueue(incoming(event("help"), settings()))
    with pytest.raises(RuntimeError, match="capacity"):
        bot.enqueue(incoming(event("status", item_id=18), settings()))
    assert list(bot.inbox) == ["simplex:1:2:17"]
