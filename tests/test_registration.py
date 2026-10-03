"""Self-registration is opt-in, ordinary-user only, private and durable."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event, Lock
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from grocery_agent.backend.config import BackendSettings
from grocery_agent.backend.passwords import verify_password
from grocery_agent.backend.repository import Conflict
from grocery_agent.persistence.control.schema import SessionRow, UserRow
from tests.application_support import NOW
from tests.test_backend_auth_refresh import PASSWORD
from tests.test_backend_auth_refresh import auth_backend as auth_backend


def test_registration_is_opt_in_and_requires_password_login(auth_backend: Any) -> None:
    _, _, _, client = auth_backend
    with pytest.raises(ValidationError, match="requires password_login_enabled"):
        BackendSettings(registration_enabled=True)
    app = client(password_login_enabled=True)
    assert not app.get("/v1/auth/capabilities").json()["registration"]
    assert (
        app.post("/v1/auth/register", json={"username": "alice", "password": PASSWORD}).status_code
        == 403
    )


def test_registration_creates_normal_account_and_owned_session(auth_backend: Any) -> None:
    repo, _, now, client = auth_backend
    app = client(password_login_enabled=True, registration_enabled=True, session_hours=1)
    assert app.get("/v1/auth/capabilities").json()["registration"]
    response = app.post("/v1/auth/register", json={"username": "alice", "password": PASSWORD})
    assert response.status_code == 201
    assert response.headers["Cache-Control"] == "no-store"
    assert "set-cookie" not in response.headers
    token = response.json()["token"]
    user = repo.authenticate(token, NOW)
    assert user.role == "user" and user.username == "alice"
    with repo.sessions() as session:
        row = session.get(UserRow, user.user_id)
        assert row and row.password_hash and verify_password(PASSWORD, row.password_hash)
        assert PASSWORD not in row.password_hash
        assert session.get(SessionRow, token) is None
    headers = {"Authorization": f"Bearer {token}"}
    assert app.get("/v1/capabilities", headers=headers).json()["registration"]
    assert app.post("/v1/invitations", headers=headers, json={"username": "bob"}).status_code == 403
    assert (
        app.post(
            "/v1/admin/refresh",
            headers={**headers, "Idempotency-Key": "refresh-test"},
            json={"source_id": "kupi"},
        ).status_code
        == 403
    )
    now[0] += timedelta(hours=1)
    assert app.get("/v1/me", headers=headers).status_code == 401


@pytest.mark.parametrize(
    "payload",
    [
        {"username": "invalid space", "password": PASSWORD},
        {"username": "", "password": PASSWORD},
        {"username": "a" * 65, "password": PASSWORD},
        {"username": "alice", "password": "a private short"[:14]},
        {"username": "alice", "password": "x" * 1025},
        {"username": "alice", "password": PASSWORD, "role": "admin"},
    ],
)
def test_registration_validates_without_echoing_password(auth_backend: Any, payload: Any) -> None:
    repo, _, _, client = auth_backend
    app = client(password_login_enabled=True, registration_enabled=True)
    response = app.post("/v1/auth/register", json=payload)
    assert response.status_code == 422
    assert payload["password"] not in response.text
    with repo.sessions() as session:
        assert session.scalar(select(func.count()).select_from(UserRow)) == 0


def test_duplicate_registration_never_changes_credentials_or_role(auth_backend: Any) -> None:
    repo, _, _, client = auth_backend
    token = repo.bootstrap("admin", NOW, password=PASSWORD)
    app = client(password_login_enabled=True, registration_enabled=True)
    response = app.post(
        "/v1/auth/register", json={"username": "admin", "password": "a different long password"}
    )
    assert response.status_code == 409
    assert repo.authenticate(token, NOW).role == "admin"
    assert (
        app.post("/v1/auth/login", json={"username": "admin", "password": PASSWORD}).status_code
        == 200
    )
    with repo.sessions() as session:
        assert session.scalar(select(func.count()).select_from(UserRow)) == 1
        assert session.scalar(select(func.count()).select_from(SessionRow)) == 2


def test_concurrent_registration_is_atomic(auth_backend: Any) -> None:
    repo, _, _, _ = auth_backend

    def register() -> str | None:
        try:
            return repo.register("alice", PASSWORD, NOW, 24)
        except Conflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        tokens = list(pool.map(lambda _: register(), range(2)))
    assert sum(token is not None for token in tokens) == 1
    with repo.sessions() as session:
        assert session.scalar(select(func.count()).select_from(UserRow)) == 1
        assert session.scalar(select(func.count()).select_from(SessionRow)) == 1


def test_registration_is_throttled_before_password_hashing(
    auth_backend: Any, monkeypatch: Any
) -> None:
    repo, _, now, client = auth_backend
    app = client(password_login_enabled=True, registration_enabled=True, auth_rate_limit=1)
    assert (
        app.post("/v1/auth/register", json={"username": "alice", "password": PASSWORD}).status_code
        == 201
    )

    def forbidden(*args: Any) -> str:
        raise AssertionError("rate-limited requests must not compute expensive password hashes")

    with monkeypatch.context() as patch:
        patch.setattr("grocery_agent.backend.repository.hash_password", forbidden)
        response = app.post("/v1/auth/register", json={"username": "bob", "password": PASSWORD})
    assert response.status_code == 429 and response.headers["Retry-After"] == "300"
    now[0] += timedelta(seconds=301)
    assert (
        app.post("/v1/auth/register", json={"username": "bob", "password": PASSWORD}).status_code
        == 201
    )
    with repo.sessions() as session:
        assert session.scalar(select(func.count()).select_from(UserRow)) == 2


def test_registered_users_cannot_read_each_others_jobs(auth_backend: Any) -> None:
    _, _, _, client = auth_backend
    app = client(password_login_enabled=True, registration_enabled=True, providers=("template",))
    headers = [
        {
            "Authorization": "Bearer "
            + app.post("/v1/auth/register", json={"username": name, "password": PASSWORD}).json()[
                "token"
            ]
        }
        for name in ("alice", "bob")
    ]
    job = app.post("/v1/recipes", headers=headers[0], json={"provider": "template"})
    assert job.status_code == 202
    assert app.get("/v1/jobs/" + job.json()["job_id"], headers=headers[1]).status_code == 404
    assert app.get("/v1/jobs", headers=headers[1]).json()["total"] == 0


def test_cookie_registration_obeys_origin_csrf_and_revocation(auth_backend: Any) -> None:
    repo, _, _, client = auth_backend
    app = client(
        password_login_enabled=True,
        registration_enabled=True,
        cookie_sessions_enabled=True,
        trusted_origin="http://localhost",
        cookie_secure=False,
    )
    payload = {"username": "alice", "password": PASSWORD, "session_mode": "cookie"}
    assert (
        app.post(
            "/v1/auth/register", json=payload, headers={"Origin": "https://untrusted.example"}
        ).status_code
        == 403
    )
    with repo.sessions() as session:
        assert session.scalar(select(func.count()).select_from(UserRow)) == 0
    response = app.post("/v1/auth/register", json=payload, headers={"Origin": "http://localhost"})
    assert response.status_code == 201 and "token" not in response.json()
    assert "HttpOnly" in response.headers["set-cookie"]
    assert app.get("/v1/auth/session").json()["user"]["role"] == "user"
    assert app.delete("/v1/session").status_code == 403
    assert (
        app.delete(
            "/v1/session", headers={"X-CSRF-Token": response.json()["csrf_token"]}
        ).status_code
        == 204
    )
    assert app.get("/v1/me").status_code == 401


def test_register_is_public_in_openapi_but_private_routes_remain_protected(
    auth_backend: Any,
) -> None:
    _, _, _, client = auth_backend
    schema = client().get("/openapi.json").json()
    assert "security" not in schema["paths"]["/v1/auth/register"]["post"]
    assert schema["paths"]["/v1/me"]["get"]["security"] == [{"BearerSession": []}]


def test_password_work_is_bounded_across_registration_and_login(monkeypatch: Any) -> None:
    from grocery_agent.backend.passwords import hash_password

    lock, full, release = Lock(), Event(), Event()
    active = peak = 0

    def derive(*args: Any, **kwargs: Any) -> bytes:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 2:
                full.set()
        try:
            assert release.wait(timeout=5)
            return bytes(32)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr("grocery_agent.backend.passwords.hashlib.scrypt", derive)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(hash_password, PASSWORD) for _ in range(2)]
        futures += [pool.submit(verify_password, PASSWORD, None) for _ in range(2)]
        try:
            assert full.wait(timeout=5)
            with lock:
                assert active == peak == 2
        finally:
            release.set()
        for future in futures:
            future.result(timeout=5)
    assert peak == 2
