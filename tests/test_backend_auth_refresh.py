"""Offline password/cookie security and durable administrator refresh controls."""

import io
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select, update

from grocery_agent.acquisition.profiles import profiled_adapter
from grocery_agent.backend.api import COOKIE_NAME, create_app
from grocery_agent.backend.config import BackendSettings
from grocery_agent.backend.passwords import hash_password, verify_password
from grocery_agent.backend.repository import (
    AccessDenied,
    ClaimedJob,
    Conflict,
    ControlRepository,
    RateLimited,
)
from grocery_agent.backend.worker import Worker
from grocery_agent.catalogue.profiles import AcquisitionProfile, Coverage
from grocery_agent.catalogue.repository import SQLAlchemyCatalogueRepository
from grocery_agent.config import Settings
from grocery_agent.persistence.control.schema import AuthRateRow, JobRow, SessionRow, UserRow
from grocery_agent.persistence.database import create_database_engine, open_database
from grocery_agent.persistence.repository import SQLAlchemyOfferRepository
from grocery_agent.persistence.schema import ScrapeRunRow
from grocery_agent.persistence.snapshots import FileSnapshotStore
from grocery_agent.stores.registry import default_registry
from tests.application_support import NOW, complete_batch

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "correct horse battery staple"


@pytest.fixture
def auth_backend(tmp_path: Path) -> Any:
    from grocery_agent.persistence.migrations import upgrade_database

    engine = create_database_engine(f"sqlite:///{tmp_path / 'control.db'}")
    upgrade_database(engine, "control")
    repo = ControlRepository(engine)
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'offers.db'}",
        meal_config=ROOT / "config/meals.toml",
        lock_path=tmp_path / "offers.lock",
        _env_file=None,
    )
    now = [NOW]

    def client(**kwargs: Any) -> TestClient:
        return TestClient(
            create_app(settings, BackendSettings(**kwargs), engine=engine, clock=lambda: now[0])
        )

    yield repo, settings, now, client
    engine.dispose()


def test_scrypt_salt_version_parameters_constant_time_and_bounds(monkeypatch: Any) -> None:
    encoded = hash_password(PASSWORD)
    assert encoded != hash_password(PASSWORD)
    assert encoded.startswith("scrypt$v1$131072$8$1$")
    assert PASSWORD not in encoded
    assert verify_password(PASSWORD, encoded)
    assert not verify_password("a different password", encoded)
    calls = []
    monkeypatch.setattr(
        "grocery_agent.backend.passwords.hashlib.scrypt",
        lambda *args, **kwargs: calls.append(kwargs) or bytes(32),
    )
    for malformed in (None, "scrypt$v2$131072$8$1$", encoded.replace("131072", "1073741824")):
        assert not verify_password(PASSWORD, malformed)
    assert all(call["n"] == 2**17 and call["maxmem"] == 160 * 1024 * 1024 for call in calls)
    for bad in ("short", "a" * 1025):
        with pytest.raises(ValueError):
            hash_password(bad)


def test_password_login_is_optional_generic_revocable_and_private(auth_backend: Any) -> None:
    repo, _, _, client = auth_backend
    app = client(password_login_enabled=True)
    invitation = repo.invite("alice", NOW)
    accepted = app.post("/v1/invitations/accept", json={"token": invitation, "password": PASSWORD})
    assert accepted.status_code == 201
    bearer = accepted.json()["token"]
    assert accepted.json()["session_mode"] == "bearer"
    assert "set-cookie" not in accepted.headers
    login = app.post("/v1/auth/login", json={"username": "alice", "password": PASSWORD})
    assert login.status_code == 200
    token = login.json()["token"]
    assert token != bearer
    response = app.get("/v1/auth/session", headers={"Authorization": f"Bearer {token}"})
    assert response.json()["user"]["username"] == "alice"
    assert "csrf_token" not in response.json()
    failures = [
        app.post("/v1/auth/login", json={"username": username, "password": "wrong long password"})
        for username in ("alice", "missing")
    ]
    assert failures[0].status_code == failures[1].status_code == 401
    assert failures[0].json() == failures[1].json()
    with repo.sessions.begin() as session:
        user = session.scalar(select(UserRow).where(UserRow.username == "alice"))
        assert user and user.password_hash and PASSWORD not in user.password_hash
        user.enabled = False
    disabled = app.post("/v1/auth/login", json={"username": "alice", "password": PASSWORD})
    assert disabled.status_code == 401 and disabled.json() == failures[0].json()
    assert app.get("/v1/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    for payload in (
        {"username": "alice", "password": "short secret"},
        {"username": "alice", "password": PASSWORD, "extra": PASSWORD},
    ):
        invalid = app.post("/v1/auth/login", json=payload)
        assert invalid.status_code == 422
        assert payload["password"] not in invalid.text
    assert (
        client()
        .post("/v1/auth/login", json={"username": "alice", "password": PASSWORD})
        .status_code
        == 403
    )


def test_legacy_invite_and_disabled_cookie_mode_preserve_compatibility(auth_backend: Any) -> None:
    repo, _, _, client = auth_backend
    app = client()
    assert app.get("/v1/auth/capabilities").json() == {
        "password_login": False,
        "registration": False,
        "cookie_sessions": False,
        "session_modes": ["bearer"],
    }
    invitation = repo.invite("legacy", NOW)
    assert (
        app.post(
            "/v1/invitations/accept", json={"token": invitation, "session_mode": "cookie"}
        ).status_code
        == 422
    )
    accepted = app.post("/v1/invitations/accept", json={"token": invitation})
    assert accepted.status_code == 201
    assert repo.authenticate(accepted.json()["token"], NOW).username == "legacy"
    reused = app.post("/v1/invitations/accept", json={"token": invitation})
    unknown = app.post("/v1/invitations/accept", json={"token": "x" * 43})
    assert reused.status_code == unknown.status_code == 401
    assert reused.json() == unknown.json()


def test_default_browser_bearer_origin_is_compatible(auth_backend: Any) -> None:
    repo, _, _, client = auth_backend
    app = client()
    origin = {"Origin": "http://testserver"}
    invitation = repo.invite("browser", NOW)
    accepted = app.post("/v1/invitations/accept", json={"token": invitation}, headers=origin)
    assert accepted.status_code == 201
    headers = {**origin, "Authorization": f"Bearer {accepted.json()['token']}"}
    assert (
        app.post("/v1/recipes", json={"provider": "template"}, headers=headers).status_code == 202
    )
    assert app.delete("/v1/session", headers=headers).status_code == 204


def test_cookie_csrf_origin_resume_bearer_precedence_and_logout(auth_backend: Any) -> None:
    repo, _, _, client = auth_backend
    options = dict(
        cookie_sessions_enabled=True,
        password_login_enabled=True,
        trusted_origin="http://localhost",
        cookie_secure=False,
    )
    app = client(**options)
    invite = repo.invite("cookie-user", NOW)
    payload = {"token": invite, "password": PASSWORD, "session_mode": "cookie"}
    assert (
        app.post(
            "/v1/invitations/accept", json=payload, headers={"Origin": "https://evil.test"}
        ).status_code
        == 403
    )
    accepted = app.post(
        "/v1/invitations/accept", json=payload, headers={"Origin": "http://localhost"}
    )
    assert accepted.status_code == 201
    csrf = accepted.json()["csrf_token"]
    assert "token" not in accepted.json()
    assert "HttpOnly" in accepted.headers["set-cookie"]
    assert "SameSite=strict" in accepted.headers["set-cookie"]
    assert "Path=/v1" in accepted.headers["set-cookie"]
    token = app.cookies.get(COOKIE_NAME)
    assert token and repo.validate_csrf(token, csrf)
    assert not repo.validate_csrf(token, token)
    restarted = client(**options)
    restarted.cookies.set(COOKIE_NAME, token)
    resumed = restarted.get("/v1/auth/session")
    assert resumed.status_code == 200 and resumed.json()["csrf_token"] == csrf
    assert resumed.headers["cache-control"] == "no-store"
    assert (
        restarted.get("/v1/auth/session", headers={"Origin": "https://evil.test"}).status_code
        == 403
    )
    assert (
        restarted.get("/v1/auth/session", headers={"Sec-Fetch-Site": "cross-site"}).status_code
        == 403
    )
    assert restarted.delete("/v1/session").status_code == 403
    assert restarted.delete("/v1/session", headers={"X-CSRF-Token": "x" * 64}).status_code == 403
    csrf_headers = {"X-CSRF-Token": csrf, "Origin": "http://localhost"}
    assert (
        restarted.post(
            "/v1/recipes", json={"provider": "template"}, headers=csrf_headers
        ).status_code
        == 202
    )
    assert (
        restarted.delete("/v1/session", headers={**csrf_headers, "Origin": "null"}).status_code
        == 403
    )
    # An explicit invalid bearer credential must never fall back to a valid cookie.
    assert (
        restarted.get("/v1/me", headers={"Authorization": "Bearer " + "x" * 43}).status_code == 401
    )
    bearer = repo.bootstrap("admin", NOW)
    assert (
        restarted.post(
            "/v1/recipes",
            json={"provider": "template"},
            headers={"Authorization": f"Bearer {bearer}"},
        ).status_code
        == 202
    )
    assert restarted.delete("/v1/session", headers=csrf_headers).status_code == 204
    assert restarted.get("/v1/me").status_code == 401
    with pytest.raises(AccessDenied):
        repo.authenticate(token, NOW)
    login = app.post(
        "/v1/auth/login",
        json={"username": "cookie-user", "password": PASSWORD, "session_mode": "cookie"},
    )
    assert login.status_code == 200 and login.json()["csrf_token"] != csrf


def test_secure_cookie_default_and_strict_origin_configuration(auth_backend: Any) -> None:
    repo, _, _, client = auth_backend
    app = client(cookie_sessions_enabled=True, trusted_origin="https://grocery.test")
    response = app.post(
        "/v1/invitations/accept",
        json={"token": repo.invite("secure", NOW), "session_mode": "cookie"},
    )
    assert response.status_code == 201 and "; Secure" in response.headers["set-cookie"]
    for options in (
        {"cookie_sessions_enabled": True},
        {"cookie_secure": False},
        {"cookie_secure": False, "trusted_origin": "http://public.test"},
        {"trusted_origin": "https://grocery.test/path"},
    ):
        with pytest.raises(ValidationError):
            BackendSettings(**options)


def test_auth_throttle_durable_bounded_and_atomic(auth_backend: Any) -> None:
    repo, _, now, client = auth_backend
    app = client(auth_rate_limit=1)
    unknown = {"token": "x" * 43}
    assert app.post("/v1/invitations/accept", json=unknown).status_code == 401
    limited = client(auth_rate_limit=1).post("/v1/invitations/accept", json=unknown)
    assert limited.status_code == 429 and limited.headers["retry-after"] == "300"
    now[0] += timedelta(seconds=300)
    assert client(auth_rate_limit=1).post("/v1/invitations/accept", json=unknown).status_code == 401

    def attempt(_: int) -> bool:
        try:
            repo.check_auth_rate("login", "concurrent", "account", NOW, limit=2)
            return True
        except RateLimited:
            return False

    with ThreadPoolExecutor(max_workers=3) as pool:
        assert sum(pool.map(attempt, range(3))) == 2
    for index in range(300):
        try:
            repo.check_auth_rate("invite", str(index), f"private-user-{index}", NOW, limit=1)
        except RateLimited:
            pass
    with repo.sessions() as session:
        assert session.scalar(select(func.count()).select_from(AuthRateRow)) <= 1026
        assert all("private-user" not in row.bucket for row in session.scalars(select(AuthRateRow)))


def test_refresh_coalescing_cooldown_receipts_restart_ownership_and_role(auth_backend: Any) -> None:
    repo, _, _, _ = auth_backend
    admin = repo.authenticate(repo.bootstrap("admin", NOW), NOW).user_id
    other = repo.authenticate(repo.bootstrap("other", NOW), NOW).user_id
    payload = {"source_id": "kupi", "profile_fingerprint": "a" * 24}
    with ThreadPoolExecutor(max_workers=3) as pool:
        jobs = list(
            pool.map(lambda i: repo.submit_refresh(admin, payload, f"key-{i}", NOW), range(3))
        )
    assert len({job.job_id for job in jobs}) == 1
    first = jobs[0]
    with pytest.raises(Conflict, match="temporarily"):
        repo.submit_refresh(other, payload, "other", NOW)
    assert repo.get(other, first.job_id) is None
    assert repo.result(other, first.job_id) is None
    with pytest.raises(Conflict, match="different"):
        repo.submit(admin, {}, "key-1", NOW)
    claim = repo.claim(NOW, 60, 3)
    assert claim
    repo.finish(claim, NOW, result={"status": "ok", "run_id": "fixture"})
    restarted = ControlRepository(repo.sessions.kw["bind"])
    assert restarted.submit_refresh(admin, payload, "new-key", NOW).job_id == first.job_id
    later = NOW + timedelta(seconds=301)
    second = restarted.submit_refresh(other, payload, "other", later)
    assert second.job_id != first.job_id
    # Old idempotent aliases keep returning the original owned receipt after cooldown.
    assert restarted.submit_refresh(admin, payload, "key-1", later).job_id == first.job_id
    with pytest.raises(Conflict, match="different"):
        restarted.submit_refresh(
            admin, {**payload, "profile_fingerprint": "b" * 24}, "key-1", later
        )
    with repo.sessions.begin() as session:
        session.execute(update(UserRow).where(UserRow.id == other).values(role="user"))
    with pytest.raises(AccessDenied):
        restarted.submit_refresh(other, payload, "new", later)
    other_claim = restarted.claim(later, 60, 3)
    assert other_claim and not restarted.authorized(other_claim)


def test_refresh_api_configured_effective_profile_selection_and_owned_results(
    auth_backend: Any,
) -> None:
    repo, _, _, client = auth_backend
    profiles = (
        AcquisitionProfile(source_id="kupi", name="one", categories=("ovoce-a-zelenina",)),
        AcquisitionProfile(source_id="kupi", name="two", categories=("pecivo",)),
    )
    fingerprints = [
        profiled_adapter(default_registry(), profile)[1].fingerprint for profile in profiles
    ]
    assert fingerprints[0] != profiles[0].fingerprint
    app = client(acquisition_profiles=profiles)
    token = repo.bootstrap("admin", NOW)
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "refresh"}
    caps = app.get("/v1/capabilities", headers=headers).json()
    assert set(caps["refresh_profiles"]["kupi"]) == set(fingerprints)
    metadata = caps["profile_metadata"]["kupi"]
    assert {item["fingerprint"] for item in metadata} == set(fingerprints)
    assert {item["name"] for item in metadata} == {"one", "two"}
    assert all(set(item) == {"name", "fingerprint", "scope", "coverage"} for item in metadata)
    for payload in (
        {"source_id": "kupi"},
        {"source_id": "tesco", "profile_fingerprint": fingerprints[0]},
        {"source_id": "kupi", "profile_fingerprint": profiles[0].fingerprint},
        {"source_id": "kupi", "profile_fingerprint": "a" * 24},
    ):
        assert app.post("/v1/admin/refresh", json=payload, headers=headers).status_code == 422
    response = app.post(
        "/v1/admin/refresh",
        headers=headers,
        json={"source_id": "kupi", "profile_fingerprint": fingerprints[0]},
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    assert app.get(f"/v1/jobs/{job_id}/result", headers=headers).status_code == 409
    claim = repo.claim(NOW, 60, 3)
    assert claim and claim.request["profile_fingerprint"] == fingerprints[0]
    result = {
        "status": "ok",
        "source_id": "kupi",
        "profile_fingerprint": fingerprints[0],
        "run_id": "fixture",
    }
    repo.finish(claim, NOW, result=result)
    assert app.get(f"/v1/jobs/{job_id}/result", headers=headers).json() == result
    other = repo.bootstrap("other", NOW)
    denied = app.post(
        "/v1/admin/refresh",
        headers={"Authorization": f"Bearer {other}", "Idempotency-Key": "other"},
        json={"source_id": "kupi", "profile_fingerprint": fingerprints[0]},
    )
    assert denied.status_code == 409 and job_id not in denied.text


@pytest.mark.parametrize("legacy", [False, True])
def test_worker_recovers_matching_success_without_acquisition(
    auth_backend: Any, candidate: Any, monkeypatch: Any, legacy: bool
) -> None:
    repo, settings, _, _ = auth_backend
    engine = open_database(settings.database_url)
    try:
        offer = {**candidate, "scope": "kupi:locality:praha"}
        from grocery_agent.models.offer import Offer

        run = complete_batch(
            SQLAlchemyOfferRepository(engine),
            FileSnapshotStore(settings.lock_path.parent / "snapshots"),
            [Offer.model_validate(offer)],
            source="kupi",
        )
        cache = SQLAlchemyCatalogueRepository(engine)
        configured = AcquisitionProfile(source_id="kupi", name="fixture")
        profile = profiled_adapter(default_registry(), configured)[1]
        if not legacy:
            cache.record_profile(
                run.run_id,
                profile,
                Coverage(profile_fingerprint=profile.fingerprint, observed=1, complete=True),
            )
        cache.publish(run.run_id)
    finally:
        engine.dispose()
    monkeypatch.setattr(
        "grocery_agent.backend.worker.acquisition_service",
        lambda *args: pytest.fail("recovery must not acquire"),
    )
    backend = BackendSettings(acquisition_profiles=() if legacy else (configured,))
    worker = Worker(repo, None, settings, backend, lambda: NOW)
    payload = {
        "source_id": "kupi",
        **({"profile_fingerprint": profile.fingerprint} if not legacy else {}),
    }
    job = ClaimedJob(
        job_id="fixture",
        user_id="fixture",
        kind="refresh",
        request=payload,
        inputs=None,
        lease_token="fixture",
    )
    result, _ = worker._refresh(job, run.run_id)
    assert result["recovered"] is True and result["run_id"] == run.run_id
    if not legacy:
        engine = create_database_engine(settings.database_url)
        try:
            from grocery_agent.catalogue.schema import CatalogueRunProfileRow

            with engine.begin() as connection:
                connection.execute(
                    update(CatalogueRunProfileRow)
                    .where(CatalogueRunProfileRow.run_id == run.run_id)
                    .values(profile_fingerprint="a" * 24)
                )
            with pytest.raises(ValueError, match="profile does not match"):
                worker._refresh(job, run.run_id)
        finally:
            engine.dispose()


@pytest.mark.parametrize("status", ["running", "failed"])
def test_worker_never_replays_recorded_incomplete_run(
    auth_backend: Any, monkeypatch: Any, status: str
) -> None:
    repo, settings, _, _ = auth_backend
    engine = open_database(settings.database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                ScrapeRunRow.__table__.insert().values(
                    id="existing", store_id="kupi", started_at=NOW, status=status
                )
            )
    finally:
        engine.dispose()
    monkeypatch.setattr(
        "grocery_agent.backend.worker.acquisition_service",
        lambda *args: pytest.fail("incomplete evidence must not replay"),
    )
    worker = Worker(repo, None, settings, BackendSettings(), lambda: NOW)
    job = ClaimedJob(
        job_id="fixture",
        user_id="fixture",
        kind="refresh",
        request={"source_id": "kupi"},
        inputs=None,
        lease_token="fixture",
    )
    with pytest.raises(ValueError, match="incomplete"):
        worker._refresh(job, "existing")


def test_richer_recipe_overrides_and_narrowed_retailer_policy(
    auth_backend: Any, monkeypatch: Any
) -> None:
    repo, _, _, client = auth_backend
    from grocery_agent.meals.catalog import MealCatalog

    catalog = MealCatalog.load(ROOT / "config/meals.toml")
    payload = catalog.model_dump()
    payload["policy"]["retailers"] = ("tesco",)
    narrowed = MealCatalog.model_validate(payload)
    monkeypatch.setattr(MealCatalog, "load", lambda *args: narrowed)
    app = client()
    headers = {"Authorization": f"Bearer {repo.bootstrap('admin', NOW)}"}
    overrides = {
        "provider": "template",
        "retailer_ids": ["tesco"],
        "allow_loyalty": False,
        "min_protein_g": "35",
        "max_kcal": "650",
        "max_minutes": 25,
        "pantry": {"rice": "500g"},
        "use_first": ["rice"],
        "seasonings_available": False,
    }
    response = app.post("/v1/recipes", json=overrides, headers=headers)
    assert response.status_code == 202, response.text
    with repo.sessions() as session:
        row = session.get(JobRow, response.json()["job_id"])
        assert row and row.request["retailer_ids"] == ["tesco"]
        assert row.request["min_protein_g"] == "35"
        assert narrowed.policy.retailers == ("tesco",)
    for invalid in (
        {"retailer_ids": ["lidl"]},
        {"source_ids": ["tesco"]},
        {"exclusions": ["rice"]},
        {"min_protein_g": 35.0},
    ):
        assert (
            app.post("/v1/recipes", json={**overrides, **invalid}, headers=headers).status_code
            == 422
        )


def test_bootstrap_password_stdin_uses_dynamic_head_and_keeps_secret_private(
    auth_backend: Any, monkeypatch: Any, capsys: Any
) -> None:
    repo, settings, _, _ = auth_backend
    from grocery_agent.apps.backend import main

    monkeypatch.setenv("GROCERY_CONTROL_DATABASE_URL", str(repo.sessions.kw["bind"].url))
    monkeypatch.setenv("GROCERY_DATABASE_URL", settings.database_url)
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    assert main(["bootstrap", "local-admin", "--password-stdin"]) == 0
    output = capsys.readouterr()
    assert PASSWORD not in output.out + output.err
    assert repo.authenticate(json.loads(output.out)["token"], NOW).role == "admin"
    assert repo.authenticate(repo.login("local-admin", PASSWORD, NOW, 24), NOW).role == "admin"


def test_backend_settings_shared_app_and_environment_precedence(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from grocery_agent.configuration import shared_config_context

    shared = tmp_path / "shared.toml"
    shared.write_text(
        "[backend]\nsession_hours = 9\nauth_rate_limit = 3\npassword_login_enabled = true\n"
    )
    app = tmp_path / "backend.toml"
    app.write_text("session_hours = 12\n")
    monkeypatch.chdir(tmp_path)
    with shared_config_context(shared):
        assert BackendSettings().session_hours == 9
        assert BackendSettings.load(app).session_hours == 12
        assert BackendSettings.load(app).auth_rate_limit == 3
        assert BackendSettings.load(app).password_login_enabled is True
        monkeypatch.setenv("GROCERY_BACKEND_SESSION_HOURS", "15")
        assert BackendSettings.load(app).session_hours == 15
        assert BackendSettings(session_hours=20).session_hours == 15
    monkeypatch.delenv("GROCERY_BACKEND_SESSION_HOURS")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "backend.toml").write_text("session_hours = 18\n")
    assert BackendSettings.load().session_hours == 18


def test_backend_cli_shared_config_and_worker_propagation(
    auth_backend: Any,
    tmp_path: Path,
    monkeypatch: Any,
    capsys: Any,
) -> None:
    repo, settings, _, _ = auth_backend
    from grocery_agent.apps import backend as command

    shared = tmp_path / "shared.toml"
    shared.write_text(
        f'[shared]\ndatabase_url = "{settings.database_url}"\n[backend]\nsession_hours = 7\n'
    )
    app = tmp_path / "backend.toml"
    app.write_text("providers = ['template']\n")
    monkeypatch.setenv("GROCERY_CONTROL_DATABASE_URL", str(repo.sessions.kw["bind"].url))
    monkeypatch.setattr(command, "utc_now", lambda: NOW)
    assert (
        command.main(
            ["--shared-config", str(shared), "--config", str(app), "bootstrap", "shared-admin"]
        )
        == 0
    )
    token = json.loads(capsys.readouterr().out)["token"]
    with repo.sessions() as session:
        from grocery_agent.backend.repository import token_hash

        row = session.get(SessionRow, token_hash(token))
        assert row and row.expires_at == NOW + timedelta(hours=7)
    arguments = []
    monkeypatch.setattr(command, "main", lambda argv: arguments.append(argv) or 0)
    monkeypatch.setattr(
        "sys.argv",
        ["grocery-worker", "--shared-config", str(shared), "--config", str(app), "--once"],
    )
    assert command.worker_main() == 0
    assert arguments == [["--config", str(app), "--shared-config", str(shared), "worker", "--once"]]
