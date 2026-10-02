from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import select, update

from grocery_agent.backend.api import create_app
from grocery_agent.backend.config import BackendSettings
from grocery_agent.backend.recipes import RecipeExecutor
from grocery_agent.backend.repository import AccessDenied, Conflict, ControlRepository, LeaseLost
from grocery_agent.backend.worker import Worker
from grocery_agent.config import Settings
from grocery_agent.persistence.control.schema import ControlBase, OutboxRow, UserRow
from grocery_agent.persistence.database import create_database_engine
from grocery_agent.persistence.migrations import upgrade_database
from grocery_agent.recipes import RecipeRequest
from tests.application_support import NOW

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def control_repo(tmp_path: Path) -> Any:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'control.db'}")
    upgrade_database(engine, "control")
    repo = ControlRepository(engine)
    yield repo
    engine.dispose()


def owner(repo: ControlRepository) -> str:
    return repo.authenticate(repo.bootstrap("admin", NOW), NOW).user_id


def test_migration_matches_orm(control_repo: ControlRepository) -> None:
    with control_repo.sessions() as session:
        diff = compare_metadata(
            MigrationContext.configure(
                session.connection(),
                opts={
                    "include_object": lambda obj, name, type_, reflected, compare_to: (
                        name != "control_schema_version"
                    )
                },
            ),
            ControlBase.metadata,
        )
        assert diff == []


def test_invite_session_expiry_revocation_and_disabled_account(
    control_repo: ControlRepository,
) -> None:
    repo = control_repo
    invitation = repo.invite("alice", NOW)
    token = repo.accept_invite(invitation, NOW, 1)
    principal = repo.authenticate(token, NOW)
    assert principal.role == "user"
    with pytest.raises(AccessDenied):
        repo.accept_invite(invitation, NOW, 1)
    with pytest.raises(AccessDenied):
        repo.authenticate(token, NOW + timedelta(hours=1))
    with repo.sessions.begin() as session:
        session.execute(
            update(UserRow).where(UserRow.id == principal.user_id).values(enabled=False)
        )
    with pytest.raises(AccessDenied):
        repo.authenticate(token, NOW)
    admin_token = repo.bootstrap("admin", NOW)
    repo.revoke(admin_token)
    with pytest.raises(AccessDenied):
        repo.authenticate(admin_token, NOW)


def test_deduplication_quota_and_concurrent_submission(control_repo: ControlRepository) -> None:
    repo = control_repo
    user = owner(repo)
    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs = list(
            pool.map(lambda _: repo.submit(user, {"servings": 2}, "event-1", NOW), range(3))
        )
    assert len({job.job_id for job in jobs}) == 1
    with pytest.raises(Conflict, match="different"):
        repo.submit(user, {"servings": 3}, "event-1", NOW)
    with pytest.raises(Conflict, match="limit"):
        repo.submit(user, {}, "event-2", NOW, max_pending=1)


def test_atomic_claim_recovery_and_fenced_completion(control_repo: ControlRepository) -> None:
    repo = control_repo
    user = owner(repo)
    job = repo.submit(user, {}, "job", NOW)
    with ThreadPoolExecutor(max_workers=3) as pool:
        claims = list(pool.map(lambda _: repo.claim(NOW, 10, 3), range(3)))
    assert sum(claim is not None for claim in claims) == 1
    first = next(claim for claim in claims if claim)
    repo.pin(first, {"run_id": "pinned"}, NOW)
    later = NOW + timedelta(seconds=11)
    second = repo.claim(later, 10, 3)
    assert second and second.job_id == job.job_id and second.inputs == {"run_id": "pinned"}
    assert not repo.heartbeat(first, later, 10)
    with pytest.raises(LeaseLost):
        repo.finish(first, later, result={"wrong": True})
    repo.finish(second, later, result={"ok": True}, html="<p>Ready</p>")
    assert repo.result(user, job.job_id) == ({"ok": True}, "<p>Ready</p>")
    with pytest.raises(LeaseLost):
        repo.finish(second, later, result={"mutated": True})


def test_cancel_and_exhausted_recovery(control_repo: ControlRepository) -> None:
    repo = control_repo
    user = owner(repo)
    cancelled = repo.submit(user, {}, "cancel", NOW)
    claim = repo.claim(NOW, 10, 1)
    assert claim
    assert repo.cancel(user, cancelled.job_id, NOW)
    with pytest.raises(LeaseLost):
        repo.finish(claim, NOW, result={})
    exhausted = repo.submit(user, {}, "exhaust", NOW)
    assert repo.claim(NOW, 10, 1)
    assert repo.claim(NOW + timedelta(seconds=11), 10, 1) is None
    state = repo.get(user, exhausted.job_id)
    assert state and state.status == "failed" and state.error_code == "attempts_exhausted"


@pytest.mark.parametrize("terminal", ["success", "cancel", "exhausted"])
def test_verified_binding_and_transactional_outbox(
    control_repo: ControlRepository, terminal: str
) -> None:
    repo = control_repo
    user = owner(repo)
    token = repo.create_binding(user, "email", "alice@example.test", NOW)
    with repo.sessions() as session:
        from grocery_agent.persistence.control.schema import ChannelBindingRow

        binding_id = session.scalar(select(ChannelBindingRow.id))
    assert binding_id
    with pytest.raises(AccessDenied):
        repo.submit(user, {}, "event", NOW, binding_id=binding_id)
    assert repo.verify_binding("email", "alice@example.test", token, NOW) == binding_id
    with pytest.raises(AccessDenied):
        repo.verify_binding("email", "alice@example.test", token, NOW)
    job = repo.submit(user, {}, "event", NOW, binding_id=binding_id)
    claim = repo.claim(NOW, 10, 1)
    assert claim
    if terminal == "cancel":
        repo.cancel(user, job.job_id, NOW)
    elif terminal == "exhausted":
        repo.claim(NOW + timedelta(seconds=11), 10, 1)
    else:
        repo.finish(claim, NOW, result={"ok": True})
    with repo.sessions() as session:
        assert session.scalar(select(OutboxRow.job_id)) == job.job_id


def test_legacy_control_migration_preserves_users_and_profiles(tmp_path: Path) -> None:
    from uuid import uuid4

    from grocery_agent.persistence.migrations.baselines import control_baseline

    engine = create_database_engine(f"sqlite:///{tmp_path / 'legacy-control.db'}")
    baseline = control_baseline()
    baseline.create_all(engine)
    try:
        with engine.begin() as connection:
            user_id = str(uuid4())
            connection.execute(
                baseline.tables["users"]
                .insert()
                .values(id=user_id, username="existing", role="user", enabled=True, created_at=NOW)
            )
            connection.execute(
                baseline.tables["user_profiles"]
                .insert()
                .values(
                    id=str(uuid4()),
                    user_id=user_id,
                    name="Dinner",
                    revision=7,
                    parameters={"pantry": {"rice": "500g"}},
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            before = {
                name: connection.execute(select(table)).mappings().all()
                for name, table in baseline.tables.items()
            }
        assert upgrade_database(engine, "control") == "control_0004"
        with engine.connect() as connection:
            after = {
                name: connection.execute(select(table)).mappings().all()
                for name, table in baseline.tables.items()
            }
        assert before == after
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_api_auth_ownership_receipts_validation_and_restart(
    control_repo: ControlRepository, tmp_path: Path
) -> None:
    repo = control_repo
    engine = repo.sessions.kw["bind"]
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'absent-offers.db'}",
        meal_config=ROOT / "config/meals.toml",
        _env_file=None,
    )
    app = create_app(settings, engine=engine, clock=lambda: NOW)
    admin = repo.bootstrap("admin", NOW)
    alice = repo.accept_invite(repo.invite("alice", NOW), NOW, 24)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            assert (await client.post("/v1/recipes", json={})).status_code == 401
            admin_headers = {"Authorization": f"Bearer {admin}", "Idempotency-Key": "recipe-1"}
            response = await client.post(
                "/v1/recipes", json={"provider": "template"}, headers=admin_headers
            )
            assert response.status_code == 202, response.text
            job_id = response.json()["job_id"]
            repeat = await client.post(
                "/v1/recipes", json={"provider": "template"}, headers=admin_headers
            )
            assert repeat.json()["job_id"] == job_id
            changed = await client.post(
                "/v1/recipes", json={"provider": "template", "servings": 3}, headers=admin_headers
            )
            assert changed.status_code == 409
            headers = {"Authorization": f"Bearer {alice}"}
            for suffix in ("", "/result", "/report"):
                assert (
                    await client.get(f"/v1/jobs/{job_id}{suffix}", headers=headers)
                ).status_code == 404
            assert (await client.delete(f"/v1/jobs/{job_id}", headers=headers)).status_code == 404
            assert (
                await client.post(
                    "/v1/admin/refresh",
                    json={"source_id": "kupi"},
                    headers={**headers, "Idempotency-Key": "refresh"},
                )
            ).status_code == 403
            assert (
                await client.post("/v1/recipes", json={"cache_policy": "no-cache"}, headers=headers)
            ).status_code == 422
            assert (
                await client.post(
                    "/v1/recipes",
                    json={"provider": "ollama", "model": "arbitrary"},
                    headers=headers,
                )
            ).status_code == 422
            assert (
                await client.post(
                    "/v1/recipes", json={"pantry": {"unknown": "5g"}}, headers=headers
                )
            ).status_code == 422
            claim = repo.claim(NOW, 60, 3)
            assert claim
            repo.finish(claim, NOW, result={"status": "ok"}, html="<p>Result</p>")
            report = await client.get(f"/v1/jobs/{job_id}/report", headers=admin_headers)
            assert (
                report.status_code == 200 and "sandbox" in report.headers["Content-Security-Policy"]
            )
    restarted = create_app(settings, engine=engine, clock=lambda: NOW)
    async with restarted.router.lifespan_context(restarted):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(restarted), base_url="http://test"
        ) as client:
            result = await client.get(f"/v1/jobs/{job_id}/result", headers=admin_headers)
            assert result.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_worker_disabled_owner_and_failure_redaction(
    control_repo: ControlRepository, tmp_path: Path
) -> None:
    repo = control_repo
    user = owner(repo)
    request = RecipeRequest(provider="template", cache_policy="cache-only")
    job = repo.submit(user, request.model_dump(mode="json"), "disabled", NOW)
    with repo.sessions.begin() as session:
        session.execute(update(UserRow).where(UserRow.id == user).values(enabled=False))
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'missing.db'}", _env_file=None)
    backend = BackendSettings()
    worker = Worker(
        repo, RecipeExecutor(settings, backend, lambda: NOW), settings, backend, lambda: NOW
    )
    assert await worker.once()
    state = repo.get(user, job.job_id)
    assert state and state.error_code == "authorization_revoked"
    with repo.sessions.begin() as session:
        session.execute(update(UserRow).where(UserRow.id == user).values(enabled=True))
    broken = repo.submit(user, request.model_dump(mode="json"), "broken", NOW)
    assert await worker.once()
    state = repo.get(user, broken.job_id)
    assert state and state.status == "failed"
    assert state.error_code in {"invalid_or_unavailable_input", "execution_failed"}
