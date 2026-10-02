from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, update

from grocery_agent.backend.api import create_app
from grocery_agent.backend.channels import ChannelRepository
from grocery_agent.backend.config import BackendSettings
from grocery_agent.backend.repository import Conflict, ControlRepository, LeaseLost
from grocery_agent.config import Settings
from grocery_agent.contracts import Capabilities
from grocery_agent.http_client import AsyncChannelClient, BackendError, ChannelClient
from grocery_agent.persistence.control.schema import JobRow, OutboxRow, UserRow
from grocery_agent.persistence.database import create_database_engine
from grocery_agent.persistence.migrations import upgrade_database
from tests.application_support import NOW


@pytest.fixture
def channels(tmp_path: Path) -> Any:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'channels.db'}")
    upgrade_database(engine, "control")
    control = ControlRepository(engine)
    user = control.authenticate(control.bootstrap("admin", NOW), NOW).user_id
    yield ChannelRepository(control), user
    engine.dispose()


def capabilities() -> Capabilities:
    from grocery_agent.recipes import RecipeRequest

    return Capabilities(
        providers=("template",),
        default_provider="template",
        models={},
        cache_policies=("cache-only",),
        default_cache_policy="cache-only",
        sources=("kupi",),
        scope="praha",
        ingredients=("rice",),
        ingredient_labels={"rice": "Rice"},
        meal_styles=("main",),
        refresh_allowed=False,
        email_enabled=True,
        simplex_enabled=True,
        recipe_request_schema=RecipeRequest.model_json_schema(),
        offer_query_schema={},
    )


def intake(
    repo: ChannelRepository, event_id: str, text: str, *, address: str = "42", now: Any = NOW
) -> dict[str, Any]:
    return repo.intake("simplex", event_id, address, text, now, capabilities(), lambda r: r, 10)


def verify(repo: ChannelRepository, user: str) -> str:
    binding = repo.request_binding(user, "simplex", "42", NOW)
    challenge = repo.claim("simplex", NOW, 30, 5)
    assert challenge
    token = challenge["text"].split("verify ")[1]
    repo.ack("simplex", challenge["delivery_id"], challenge["lease_token"], NOW, None, 5)
    result = intake(repo, "verification", f"verify {token}")
    assert result["reply"].startswith("Channel verified")
    # Consume the verification acknowledgement before testing recipe deliveries.
    reply = repo.claim("simplex", NOW, 30, 5)
    assert reply
    repo.ack("simplex", reply["delivery_id"], reply["lease_token"], NOW, None, 5)
    return binding


def test_challenge_only_transport_can_verify_and_binding_owner_isolation(channels: Any) -> None:
    repo, user = channels
    binding = repo.request_binding(user, "email", "Alice@EXAMPLE.test", NOW)
    assert repo.bindings(user)[0]["address"] == "Alice@example.test"
    other = repo.control.authenticate(
        repo.control.accept_invite(repo.control.invite("bob", NOW), NOW, 24), NOW
    ).user_id
    with pytest.raises(Conflict):
        repo.request_binding(other, "email", "Alice@example.test", NOW)
    assert not repo.revoke(other, binding)
    assert intake(repo, "unbound", "help")["status"] == "ignored"
    delivery = repo.claim("email", NOW, 10, 3)
    assert delivery and not repo.bindings(user)[0]["verified"]
    token = delivery["text"].split("verify ")[1]
    expired = repo.intake(
        "email",
        "expired",
        "Alice@example.test",
        f"verify {token}",
        NOW + timedelta(minutes=16),
        capabilities(),
        lambda r: r,
        10,
    )
    assert expired["reply"] is None
    assert repo.revoke(user, binding)
    assert repo.claim("email", NOW + timedelta(seconds=11), 10, 3) is None


def test_confirmation_atomic_duplicate_events_retries_and_private_results(channels: Any) -> None:
    repo, user = channels
    verify(repo, user)
    text = "recipe have=rice=500g budget=80.0000"
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: intake(repo, "recipe", text), range(3)))
    assert sorted(row["status"] for row in results) == ["duplicate", "duplicate", "processed"]
    reply = results[0]["reply"]
    assert "80.0000" in reply and "500g" in reply
    token = reply.split("confirm ")[1].split()[0]
    with repo.sessions() as session:
        assert session.scalar(select(func.count()).select_from(JobRow)) == 0
    with pytest.raises(Conflict):
        intake(repo, "recipe", "recipe servings=3")
    with ThreadPoolExecutor(max_workers=3) as pool:
        confirmations = list(
            pool.map(lambda i: intake(repo, f"confirmation-{i}", f"confirm {token}"), range(3))
        )
    assert sum(row["reply"].startswith("Accepted job") for row in confirmations) == 1
    with repo.sessions() as session:
        jobs = session.scalars(select(JobRow)).all()
        assert len(jobs) == 1
        job_id = jobs[0].id
        assert jobs[0].request["pantry"] == {"rice": "500g"}
        assert jobs[0].request["max_cost_per_serving_czk"] == "80.0000"
    claimed = repo.control.claim(NOW, 30, 3)
    assert claimed
    repo.control.finish(claimed, NOW, result={"exact": "80.0000"}, html="<p>Private</p>")
    reports = []
    for _ in range(10):
        delivery = repo.claim("simplex", NOW, 30, 5)
        if delivery is None:
            break
        if delivery["result"]:
            reports.append(delivery)
        repo.ack("simplex", delivery["delivery_id"], delivery["lease_token"], NOW, None, 5)
    assert len(reports) == 1 and reports[0]["report_html"] == "<p>Private</p>"
    assert job_id in reports[0]["text"]
    assert intake(repo, "stranger-status", f"status {job_id}", address="43")["status"] == "ignored"


def test_outbox_lease_recovery_error_redaction_backoff_and_revocation(channels: Any) -> None:
    repo, user = channels
    binding = verify(repo, user)
    intake(repo, "help", "help")
    first = repo.claim("simplex", NOW, 10, 3)
    assert first and repo.claim("simplex", NOW, 10, 3) is None
    later = NOW + timedelta(seconds=11)
    restarted = ChannelRepository(repo.control)
    second = restarted.claim("simplex", later, 10, 3)
    assert second and second["delivery_id"] == first["delivery_id"]
    with pytest.raises(LeaseLost):
        repo.ack("simplex", first["delivery_id"], first["lease_token"], later, None, 3)
    with pytest.raises(LeaseLost):
        repo.ack("email", second["delivery_id"], second["lease_token"], later, None, 3)
    repo.ack("simplex", second["delivery_id"], second["lease_token"], later, "secret", 3)
    assert repo.claim("simplex", later, 10, 3) is None
    third = repo.claim("simplex", later + timedelta(seconds=4), 10, 3)
    assert third
    repo.ack(
        "simplex",
        third["delivery_id"],
        third["lease_token"],
        later + timedelta(seconds=4),
        "secret",
        3,
    )
    with repo.sessions() as session:
        row = session.get(OutboxRow, third["delivery_id"])
        assert row and row.state == "failed" and row.error_code == "transport_failed"
        assert row.payload is None
    intake(repo, "help-again", "help")
    repo.revoke(user, binding)
    assert repo.claim("simplex", NOW, 10, 3) is None


def test_disabled_owner_and_rotated_challenge_discard_old_proofs(channels: Any) -> None:
    repo, user = channels
    repo.request_binding(user, "simplex", "42", NOW)
    old = repo.claim("simplex", NOW, 10, 3)
    assert old
    repo.request_binding(user, "simplex", "42", NOW)
    assert intake(repo, "old-proof", "verify " + old["text"].split("verify ")[1])["reply"] is None
    fresh = repo.claim("simplex", NOW, 10, 3)
    assert fresh and fresh["delivery_id"] != old["delivery_id"]
    with repo.sessions.begin() as session:
        session.execute(update(UserRow).where(UserRow.id == user).values(enabled=False))
    assert intake(repo, "disabled", "help")["status"] == "ignored"
    assert repo.claim("simplex", NOW + timedelta(seconds=11), 10, 3) is None


def test_delivery_claims_are_atomic_and_confirmation_is_bound_to_one_identity(
    channels: Any,
) -> None:
    repo, user = channels
    verify(repo, user)
    reply = intake(repo, "recipe", "recipe")["reply"]
    confirmation = reply.split("confirm ")[1].split()[0]
    other = repo.control.authenticate(
        repo.control.accept_invite(repo.control.invite("bob", NOW), NOW, 24), NOW
    ).user_id
    proof = repo.control.create_binding(other, "simplex", "43", NOW)
    repo.control.verify_binding("simplex", "43", proof, NOW)
    result = intake(repo, "other-confirmation", f"confirm {confirmation}", address="43")
    assert result["reply"].startswith("Command rejected")
    with repo.sessions() as session:
        assert session.scalar(select(func.count()).select_from(JobRow)) == 0
    # Other identity's error reply is a separate destination; claims never repeat one lease.
    with ThreadPoolExecutor(max_workers=3) as pool:
        deliveries = list(pool.map(lambda _: repo.claim("simplex", NOW, 10, 3), range(3)))
    claimed = [delivery for delivery in deliveries if delivery]
    assert len(claimed) == 2 and len({row["delivery_id"] for row in claimed}) == 2


@pytest.mark.asyncio
async def test_transport_api_credentials_scope_defaults_bindings_and_contract(
    channels: Any, tmp_path: Path
) -> None:
    repo, _ = channels
    engine = repo.sessions.kw["bind"]
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'offers.db'}", _env_file=None)
    token = "channel-email-token-123456789"
    backend = BackendSettings(email_enabled=True, email_transport_token=token)
    app = create_app(settings, backend, engine=engine, clock=lambda: NOW)
    admin = repo.control.bootstrap("admin", NOW)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {admin}"},
        ) as user:
            reply = await user.post(
                "/v1/bindings", json={"channel": "email", "address": "alice@example.test"}
            )
            assert reply.status_code == 202 and "token" not in reply.text
            assert (await user.get("/v1/bindings")).json()[0]["verified"] is False
            assert (await user.post("/v1/transports/email/outbox/claim")).status_code == 401
            assert (
                await user.post("/v1/bindings", json={"channel": "simplex", "address": "42"})
            ).status_code == 403
        async with AsyncChannelClient(
            "http://test", token, "email", transport=httpx.ASGITransport(app)
        ) as client:
            first = await client.claim()
            assert first
            challenge = first["text"].split("verify ")[1]
            await client.ack(first["delivery_id"], first["lease_token"])
            verified = await client.intake("1", "alice@example.test", f"verify {challenge}")
            assert verified["reply"].startswith("Channel verified")
            assert (await client.intake("1", "alice@example.test", f"verify {challenge}"))[
                "status"
            ] == "duplicate"
            delivery = await client.claim()
            assert delivery
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {token}"},
        ) as transport:
            assert (await transport.get("/v1/me")).status_code == 401
            assert (await transport.post("/v1/transports/simplex/outbox/claim")).status_code == 401
            assert (
                await transport.post(
                    "/v1/transports/email/outbox/x/ack",
                    json={"lease_token": "not-the-right-lease-token"},
                )
            ).status_code == 409
        defaults = create_app(settings, engine=engine, clock=lambda: NOW)
        async with AsyncChannelClient(
            "http://test", token, "email", transport=httpx.ASGITransport(defaults)
        ) as client:
            with pytest.raises(BackendError, match="Invalid or expired session"):
                await client.claim()


def test_sync_transport_client_injectable_and_secret_configuration(tmp_path: Path) -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("claim"):
            return httpx.Response(200, content="null", headers={"Content-Type": "application/json"})
        if request.url.path.endswith("ack"):
            return httpx.Response(204)
        return httpx.Response(200, json={"status": "ignored", "reply": None})

    with ChannelClient(
        "http://test", "private", "email", transport=httpx.MockTransport(handler)
    ) as client:
        assert client.claim() is None
        assert client.intake("uid-1", "alice@example.test", "help")["status"] == "ignored"
        client.ack("id", "lease")
    assert all(row.headers["Authorization"] == "Bearer private" for row in requests)
    assert "private" not in str(requests[0].url)
    with pytest.raises(ValueError):
        ChannelClient("http://test", "private", "unsupported")
    # TOML cannot contain service credentials; environment settings are the credential source.
    config = tmp_path / "backend.toml"
    config.write_text('email_transport_token="must-not-load"\n')
    with pytest.raises(ValueError, match="environment"):
        BackendSettings.load(config)
    assert (
        "email_transport_token"
        not in BackendSettings(email_transport_token="private-transport-secret-token").model_dump()
    )
    assert (
        BackendSettings(email_enabled=True, email_transport_token="").email_transport_token is None
    )


def test_channel_migration_preserves_existing_jobs_and_pending_outbox(tmp_path: Path) -> None:
    from alembic import command

    from grocery_agent.persistence.migrations import migration_config

    engine = create_database_engine(f"sqlite:///{tmp_path / 'old-control.db'}")
    try:
        config = migration_config("control")
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "control_0002")
            from sqlalchemy import text

            connection.execute(
                text(
                    "INSERT INTO users (id, username, role, enabled, created_at) "
                    "VALUES ('owner', 'existing', 'user', 1, :now)"
                ),
                {"now": NOW.isoformat()},
            )
            connection.execute(
                text(
                    "INSERT INTO channel_bindings "
                    "(id,user_id,channel,address,token_hash,expires_at,verified,enabled) "
                    "VALUES ('binding','owner','simplex','42','hash',:now,1,1)"
                ),
                {"now": NOW.isoformat()},
            )
            connection.execute(
                text(
                    "INSERT INTO jobs (id,user_id,idempotency_key,kind,request,"
                    "state,phase,attempts,created_at,updated_at) VALUES "
                    "('job','owner','key','recipe','{}','succeeded','succeeded',1,:now,:now)"
                ),
                {"now": NOW.isoformat()},
            )
            connection.execute(
                text(
                    "INSERT INTO notification_outbox "
                    "(id,job_id,binding_id,state,attempts,created_at) VALUES "
                    "('delivery','job','binding','pending',0,:now)"
                ),
                {"now": NOW.isoformat()},
            )
        assert upgrade_database(engine, "control") == "control_0004"
        repository = ControlRepository(engine)
        with repository.sessions() as session:
            row = session.get(OutboxRow, "delivery")
            assert row and (row.job_id, row.binding_id, row.state, row.attempts) == (
                "job",
                "binding",
                "pending",
                0,
            )
            assert row.payload is None and row.lease_token is None
        delivery = ChannelRepository(repository).claim("simplex", NOW, 10, 3)
        assert delivery and delivery["delivery_id"] == "delivery"
    finally:
        engine.dispose()


def test_email_intake_confirmation_worker_report_delivery_end_to_end(channels: Any) -> None:
    from email.message import EmailMessage

    from grocery_agent.channels.email import (
        EmailConfig,
        EmailCredentials,
        delivery_once,
        intake_once,
    )

    repo, user = channels
    config = EmailConfig(
        enabled=True,
        intake_enabled=True,
        delivery_enabled=True,
        imap_host="imap.example.test",
        smtp_host="smtp.example.test",
        sender="agent@example.test",
        trusted_authserv_id="mx.example.test",
        trusted_ingress=True,
        trusted_senders=("alice@example.test",),
    )
    credentials = EmailCredentials("private", "imap", "private", "smtp", "private")
    address = "alice@example.test"
    repo.request_binding(user, "email", address, NOW)

    class API:
        def intake(self, event_id: str, address: str, text: str) -> dict[str, Any]:
            return repo.intake(
                "email", event_id, address, text, NOW, capabilities(), lambda r: r, 10
            )

        def claim(self) -> dict[str, Any] | None:
            return repo.claim("email", NOW, 30, 5)

        def ack(self, delivery_id: str, lease_token: str, *, error: str | None = None) -> None:
            repo.ack("email", delivery_id, lease_token, NOW, error, 5)

    class Mailer:
        def __init__(self) -> None:
            self.sent: list[EmailMessage] = []

        def send(self, message: EmailMessage, sender: str, recipient: str) -> None:
            assert recipient == address and sender == config.sender
            self.sent.append(message)

    class Inbox:
        uidvalidity = "123"

        def __init__(self, uid: str, command: str) -> None:
            self.uid = uid
            self.command = command
            self.seen: list[str] = []

        def messages(self, limit: int, max_bytes: int) -> Any:
            incoming = EmailMessage()
            incoming["From"] = address
            incoming["Authentication-Results"] = (
                "mx.example.test; dmarc=pass header.from=example.test"
            )
            incoming.set_content(self.command)
            yield self.uid, incoming.as_bytes()

        def mark_seen(self, uid: str) -> None:
            self.seen.append(uid)

    api, mailer = API(), Mailer()
    assert delivery_once(config, api, mailer) == 0
    proof = mailer.sent[-1].get_content().split("verify ")[1].strip()
    verified = Inbox("1", f"verify {proof}")
    assert intake_once(config, credentials, api, verified) == 0 and verified.seen == ["1"]
    assert delivery_once(config, api, mailer) == 0
    recipe = Inbox("2", "recipe have=rice=500g budget=80.0000")
    assert intake_once(config, credentials, api, recipe) == 0
    assert intake_once(config, credentials, api, recipe) == 0  # Lost Seen write/replay.
    assert delivery_once(config, api, mailer) == 0
    confirmation = mailer.sent[-1].get_content().split("confirm ")[1].split()[0]
    assert intake_once(config, credentials, api, Inbox("3", f"confirm {confirmation}")) == 0
    assert delivery_once(config, api, mailer) == 0
    claimed = repo.control.claim(NOW, 30, 3)
    assert claimed
    repo.control.finish(claimed, NOW, result={"cost": "80.0000"}, html="<p>Private recipe</p>")
    assert delivery_once(config, api, mailer) == 0
    attachments = list(mailer.sent[-1].iter_attachments())
    assert [part.get_filename() for part in attachments] == ["report.html", "result.json"]
    assert b"Private recipe" in attachments[0].get_payload(decode=True)
    assert b"80.0000" in attachments[1].get_payload(decode=True)
    assert repo.claim("email", NOW, 30, 5) is None
