"""Configurable account access and owned durable jobs, composed with catalogue routes."""

import hmac
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError

from grocery_agent.acquisition.profiles import profiled_adapter
from grocery_agent.catalogue.api import create_app as catalogue_app
from grocery_agent.catalogue.models import CatalogueQuery
from grocery_agent.config import Settings
from grocery_agent.contracts import Capabilities, JobPage
from grocery_agent.meals.catalog import MealCatalog
from grocery_agent.models.common import DomainModel, StoreId, utc_now
from grocery_agent.persistence.control.config import ControlSettings
from grocery_agent.persistence.database import create_database_engine
from grocery_agent.persistence.migrations import migration_config, schema_version
from grocery_agent.recipes import RecipeRequest
from grocery_agent.stores.registry import default_registry

from .config import BackendSettings
from .passwords import password_bytes
from .recipes import effective_catalog
from .repository import AccessDenied, Conflict, ControlRepository, JobView, Principal, RateLimited

COOKIE_NAME = "grocery_session"
PUBLIC_AUTH = {
    "/v1/auth/login",
    "/v1/auth/register",
    "/v1/invitations/accept",
    "/v1/auth/capabilities",
}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class InvitationInput(DomainModel):
    username: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")]


class TokenInput(DomainModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    token: Annotated[str, Field(min_length=20, max_length=200)]
    password: SecretStr | None = None
    session_mode: Literal["bearer", "cookie"] = "bearer"

    @field_validator("password")
    @classmethod
    def bounded_password(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            password_bytes(value.get_secret_value())
        return value


class LoginInput(DomainModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    username: Annotated[str, Field(min_length=1, max_length=64)]
    password: SecretStr
    session_mode: Literal["bearer", "cookie"] = "bearer"

    @field_validator("password")
    @classmethod
    def bounded_password(cls, value: SecretStr) -> SecretStr:
        password_bytes(value.get_secret_value())
        return value


class RegistrationInput(LoginInput):
    username: Annotated[str, Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")]


class RefreshInput(DomainModel):
    source_id: StoreId
    profile_fingerprint: Annotated[str | None, Field(pattern=r"^[a-f0-9]{24}$")] = None


def create_app(
    settings: Settings | None = None,
    backend: BackendSettings | None = None,
    control: ControlSettings | None = None,
    *,
    engine: Engine | None = None,
    clock: Callable[[], datetime] = utc_now,
) -> FastAPI:
    from alembic.script import ScriptDirectory

    settings, backend = settings or Settings(), backend or BackendSettings()
    control = control or ControlSettings()
    if settings.database_url == control.database_url:
        raise ValueError("offer and control databases must be separate")
    owned_engine = engine is None
    engine = engine or create_database_engine(control.database_url)
    repository = ControlRepository(engine)
    catalog = MealCatalog.load(settings.meal_config)
    source_scopes = backend.allowed_source_scopes(catalog.policy.source_id, catalog.policy.scope)
    app = catalogue_app(settings, clock=clock)
    old_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> Any:
        try:
            head = ScriptDirectory.from_config(migration_config("control")).get_current_head()
            if schema_version(engine, "control") != head:
                raise ValueError(
                    "control schema needs migration; run grocery-agent db upgrade control"
                )
            async with old_lifespan(application):
                yield
        finally:
            if owned_engine:
                engine.dispose()

    app.router.lifespan_context = lifespan
    app.title = "Grocery backend"
    app.router.routes = [
        route for route in app.router.routes if getattr(route, "path", "") != "/v1/capabilities"
    ]

    @app.middleware("http")
    async def authenticate(request: Request, call_next: Any) -> Response:
        api_request = request.url.path.startswith("/v1/")
        if api_request and backend.cookie_sessions_enabled and request.method not in SAFE_METHODS:
            origin = request.headers.get("Origin")
            if origin is not None and origin != backend.trusted_origin:
                return JSONResponse(
                    {"detail": "Untrusted origin"},
                    status_code=403,
                    headers={"Cache-Control": "no-store"},
                )
        if api_request and request.url.path not in PUBLIC_AUTH:
            scheme, _, token = request.headers.get("Authorization", "").partition(" ")
            cookie_mode = False
            if not request.headers.get("Authorization") and backend.cookie_sessions_enabled:
                token = request.cookies.get(COOKIE_NAME, "")
                scheme = "bearer" if token else ""
                cookie_mode = bool(token)
            if scheme.casefold() != "bearer" or not 20 <= len(token) <= 200:
                return JSONResponse(
                    {"detail": "Bearer session required"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
                )
            try:
                if request.url.path.startswith("/v1/transports/"):
                    if cookie_mode:
                        raise AccessDenied("transport credential required")
                    from grocery_agent.backend.channel_api import authenticate_transport

                    authenticate_transport(backend, request.url.path, token)
                else:
                    request.state.principal = repository.authenticate(token, clock())
                    request.state.session_token = token
                    request.state.cookie_mode = cookie_mode
                    if cookie_mode and request.method not in SAFE_METHODS:
                        if not repository.validate_csrf(
                            token, request.headers.get("X-CSRF-Token", "")
                        ):
                            return JSONResponse(
                                {"detail": "CSRF token required"},
                                status_code=403,
                                headers={"Cache-Control": "no-store"},
                            )
            except AccessDenied:
                return JSONResponse(
                    {"detail": "Invalid or expired session"},
                    status_code=401,
                    headers={"Cache-Control": "no-store"},
                )
            except SQLAlchemyError:
                return JSONResponse({"detail": "Control storage unavailable"}, status_code=503)
        response: Response = await call_next(request)
        if request.url.path.startswith("/v1/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    # FastAPI's default validation errors include raw input, including passwords.
    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request: Request, exc: RequestValidationError) -> JSONResponse:
        if request.url.path in PUBLIC_AUTH:
            return JSONResponse({"detail": "Invalid authentication request"}, status_code=422)
        return JSONResponse(
            {
                "detail": [
                    {key: value for key, value in error.items() if key in {"type", "loc", "msg"}}
                    for error in exc.errors()
                ]
            },
            status_code=422,
        )

    @app.exception_handler(RateLimited)
    async def rate_limited(request: Request, exc: RateLimited) -> JSONResponse:
        return JSONResponse(
            {"detail": "Authentication temporarily unavailable"},
            status_code=429,
            headers={"Retry-After": str(backend.auth_rate_window_seconds)},
        )

    @app.exception_handler(SQLAlchemyError)
    async def storage_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        return JSONResponse({"detail": "Control storage unavailable"}, status_code=503)

    @app.exception_handler(Conflict)
    async def conflict(request: Request, exc: Conflict) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(AccessDenied)
    async def denied(request: Request, exc: AccessDenied) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=403)

    def principal(request: Request) -> Principal:
        return request.state.principal  # type: ignore[no-any-return]

    @app.get("/v1/me")
    def me(user: Annotated[Principal, Depends(principal)]) -> Principal:
        return user

    @app.delete("/v1/session", status_code=204)
    def logout(request: Request, user: Annotated[Principal, Depends(principal)]) -> Response:
        repository.revoke(request.state.session_token)
        response = Response(status_code=204)
        if request.state.cookie_mode:
            response.delete_cookie(
                COOKIE_NAME,
                path="/v1",
                secure=backend.cookie_secure,
                httponly=True,
                samesite="strict",
            )
        return response

    @app.post("/v1/invitations", status_code=201)
    def invite(
        payload: InvitationInput, user: Annotated[Principal, Depends(principal)]
    ) -> dict[str, str]:
        if user.role != "admin":
            raise HTTPException(403, "Administrator required")
        return {"token": repository.invite(payload.username, clock())}

    @app.post("/v1/invitations/accept", status_code=201)
    def accept(payload: TokenInput, request: Request, response: Response) -> dict[str, str]:
        check_mode(payload.session_mode)
        if payload.password is not None and not backend.password_login_enabled:
            raise HTTPException(422, "Password login is disabled")
        throttle(request, "invite", payload.token)
        try:
            token = repository.accept_invite(
                payload.token,
                clock(),
                backend.session_hours,
                password=payload.password.get_secret_value() if payload.password else None,
            )
        except AccessDenied:
            raise HTTPException(401, "Invalid authentication credentials") from None
        return session_response(token, payload.session_mode, response)

    def auth_capabilities() -> dict[str, Any]:
        return {
            "password_login": backend.password_login_enabled,
            "registration": backend.registration_enabled,
            "cookie_sessions": backend.cookie_sessions_enabled,
            "session_modes": ["bearer", "cookie"]
            if backend.cookie_sessions_enabled
            else ["bearer"],
        }

    app.get("/v1/auth/capabilities")(auth_capabilities)

    def check_mode(mode: str) -> None:
        if mode == "cookie" and not backend.cookie_sessions_enabled:
            raise HTTPException(422, "Cookie sessions are disabled")

    def throttle(request: Request, action: str, identity: str) -> None:
        repository.check_auth_rate(
            action,
            request.client.host if request.client else "unknown",
            identity,
            clock(),
            limit=backend.auth_rate_limit,
            window_seconds=backend.auth_rate_window_seconds,
        )

    def session_response(token: str, mode: str, response: Response) -> dict[str, str]:
        if mode == "cookie":
            csrf_token = csrf_for_session(token)
            repository.set_csrf(token, csrf_token)
            response.set_cookie(
                COOKIE_NAME,
                token,
                max_age=backend.session_hours * 3600,
                path="/v1",
                secure=backend.cookie_secure,
                httponly=True,
                samesite="strict",
            )
            return {"csrf_token": csrf_token, "session_mode": "cookie"}
        return {"token": token, "token_type": "bearer", "session_mode": "bearer"}

    def csrf_for_session(token: str) -> str:
        # The session bearer secret is the HMAC key; the CSRF value cannot authenticate.
        # Its digest is stored separately and verified on every unsafe cookie request.
        return hmac.new(token.encode(), b"grocery-cookie-csrf-v1", "sha256").hexdigest()

    @app.get("/v1/auth/session")
    def current_session(
        request: Request, user: Annotated[Principal, Depends(principal)]
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            "user": user.model_dump(),
            "session_mode": "cookie" if request.state.cookie_mode else "bearer",
        }
        if request.state.cookie_mode:
            origin = request.headers.get("Origin")
            if (origin is not None and origin != backend.trusted_origin) or request.headers.get(
                "Sec-Fetch-Site"
            ) == "cross-site":
                raise HTTPException(403, "Untrusted origin")
            csrf = csrf_for_session(request.state.session_token)
            if not repository.validate_csrf(request.state.session_token, csrf):
                raise HTTPException(401, "Invalid or expired session")
            data["csrf_token"] = csrf
        return data

    @app.post("/v1/auth/login")
    def login(payload: LoginInput, request: Request, response: Response) -> dict[str, str]:
        check_mode(payload.session_mode)
        if not backend.password_login_enabled:
            raise HTTPException(403, "Password login is disabled")
        throttle(request, "login", payload.username)
        try:
            token = repository.login(
                payload.username,
                payload.password.get_secret_value(),
                clock(),
                backend.session_hours,
            )
        except AccessDenied:
            raise HTTPException(401, "Invalid authentication credentials") from None
        return session_response(token, payload.session_mode, response)

    @app.post("/v1/auth/register", status_code=201)
    def register(
        payload: RegistrationInput, request: Request, response: Response
    ) -> dict[str, str]:
        if not backend.registration_enabled:
            raise HTTPException(403, "Self-registration is disabled")
        check_mode(payload.session_mode)
        throttle(request, "register", payload.username)
        token = repository.register(
            payload.username,
            payload.password.get_secret_value(),
            clock(),
            backend.session_hours,
        )
        return session_response(token, payload.session_mode, response)

    @app.get("/v1/capabilities", response_model=Capabilities)
    def capabilities(user: Annotated[Principal, Depends(principal)]) -> Capabilities:
        return Capabilities.model_validate(
            {
                **{
                    key: value
                    for key, value in auth_capabilities().items()
                    if key in Capabilities.model_fields
                },
                **(
                    {"refresh_profiles": refresh_profiles()}
                    if "refresh_profiles" in Capabilities.model_fields
                    else {}
                ),
                "providers": list(backend.providers),
                "default_provider": "codex"
                if "codex" in backend.providers
                else next(iter(backend.providers), None),
                "models": {
                    "codex": list(backend.codex_models),
                    "ollama": list(backend.ollama_models),
                },
                "cache_policies": list(backend.cache_policies),
                "default_cache_policy": backend.default_cache_policy,
                "sources": list(source_scopes),
                "source_scopes": source_scopes,
                "combined_sources": len(source_scopes) > 1,
                "scope": catalog.policy.scope,
                "location_label": catalog.policy.location_label,
                "ingredients": sorted(catalog.ingredients),
                "ingredient_labels": {
                    key: value.label for key, value in catalog.ingredients.items()
                },
                "meal_styles": ["main", "breakfast", "snack"],
                "refresh_allowed": user.role == "admin"
                and (
                    not backend.source_scopes
                    and not backend.acquisition_profiles
                    or any(
                        profile.source_id in source_scopes
                        and (
                            profile.scope is None
                            or profile.scope in source_scopes[profile.source_id]
                        )
                        for profile in backend.acquisition_profiles
                    )
                ),
                "email_enabled": backend.email_enabled
                and backend.email_transport_token is not None,
                "simplex_enabled": backend.simplex_enabled
                and backend.simplex_transport_token is not None,
                "recipe_request_schema": RecipeRequest.model_json_schema(),
                "offer_query_schema": CatalogueQuery.model_json_schema(),
            }
        )

    def refresh_profiles() -> dict[str, tuple[str, ...]]:
        result: dict[str, list[str]] = {}
        for profile in backend.acquisition_profiles:
            if profile.source_id not in source_scopes or (
                profile.scope is not None and profile.scope not in source_scopes[profile.source_id]
            ):
                continue
            try:
                _, resolved = profiled_adapter(default_registry(), profile)
            except ValueError:
                continue
            result.setdefault(profile.source_id, []).append(resolved.fingerprint)
        return {source: tuple(sorted(set(fingerprints))) for source, fingerprints in result.items()}

    @app.get("/v1/jobs", response_model=JobPage)
    def jobs(
        user: Annotated[Principal, Depends(principal)],
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ) -> JobPage:
        return repository.list_jobs(user.user_id, offset=offset, limit=limit)

    def validate_request(payload: RecipeRequest) -> RecipeRequest:
        if "cache_policy" not in payload.model_fields_set:
            payload = RecipeRequest.model_validate(
                {**payload.model_dump(), "cache_policy": backend.default_cache_policy}
            )
        if (
            payload.provider not in backend.providers
            or payload.cache_policy not in backend.cache_policies
        ):
            raise HTTPException(
                422, "Provider or cache policy is not permitted; check capabilities"
            )
        allowed = backend.ollama_models if payload.provider == "ollama" else backend.codex_models
        if (payload.provider == "ollama" and payload.model not in allowed) or (
            payload.model is not None
            and (payload.provider == "template" or payload.model not in allowed)
        ):
            raise HTTPException(422, "Model is not configured on this server")
        if (
            not payload.source_ids
            or len(set(payload.source_ids)) != len(payload.source_ids)
            or not set(payload.source_ids) <= source_scopes.keys()
        ):
            raise HTTPException(422, "Sources must be unique configured source IDs")
        fingerprints = payload.profile_fingerprints
        if not set(fingerprints) <= set(payload.source_ids):
            raise HTTPException(422, "Profile fingerprints must belong to requested sources")
        if payload.profile_fingerprint is not None:
            if len(payload.source_ids) != 1:
                raise HTTPException(422, "Scalar profile fingerprint requires a single source")
            source = payload.source_ids[0]
            if source in fingerprints and fingerprints[source] != payload.profile_fingerprint:
                raise HTTPException(422, "Conflicting profile fingerprints")
            fingerprints = {**fingerprints, source: payload.profile_fingerprint}
        if payload.cache_policy != "cache-only":
            for source in payload.source_ids:
                profiles = [
                    profile
                    for profile in backend.acquisition_profiles
                    if profile.source_id == source
                    and (profile.scope is None or profile.scope in source_scopes[source])
                ]
                fingerprint = fingerprints.get(source)
                if fingerprint is None:
                    profiles = [profile for profile in profiles if profile.name == "default"]
                try:
                    profiles = [
                        resolved
                        for profile in profiles
                        for _, resolved in [profiled_adapter(default_registry(), profile)]
                        if fingerprint is None or resolved.fingerprint == fingerprint
                    ]
                except ValueError as exc:
                    raise HTTPException(422, str(exc)) from exc
                if len(profiles) != 1:
                    raise HTTPException(
                        422,
                        "Acquisition requires an unambiguous configured profile for each source",
                    )
        try:
            # Validate meal parameters only. Source permissions are checked above;
            # catalogue preparation and any acquisition belong to the worker.
            effective_catalog(catalog, payload, source_scopes=source_scopes)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return payload

    from grocery_agent.backend.channel_api import mount_channels

    mount_channels(app, repository, backend, clock, principal, capabilities, validate_request)

    @app.post("/v1/recipes", status_code=202, response_model=JobView)
    def submit(
        payload: RecipeRequest,
        user: Annotated[Principal, Depends(principal)],
        idempotency_key: Annotated[str | None, Header(min_length=1, max_length=200)] = None,
    ) -> JobView:
        payload = validate_request(payload)
        key = idempotency_key or str(payload.request_id)
        normalized = payload.model_dump(mode="json", exclude={"request_id"})
        # Use a request ID derived from the owned idempotency key so retries without an
        # explicit request_id compare identically while different users stay isolated.
        from uuid import NAMESPACE_URL, uuid5

        normalized["request_id"] = str(uuid5(NAMESPACE_URL, f"{user.user_id}/{key}"))
        return repository.submit(
            user.user_id, normalized, key, clock(), max_pending=backend.max_pending_per_user
        )

    @app.post("/v1/admin/refresh", status_code=202, response_model=JobView)
    def refresh(
        payload: RefreshInput,
        user: Annotated[Principal, Depends(principal)],
        idempotency_key: Annotated[str, Header(min_length=1, max_length=200)],
    ) -> JobView:
        if user.role != "admin":
            raise HTTPException(403, "Administrator required")
        profiles = [
            profile
            for profile in backend.acquisition_profiles
            if profile.source_id == payload.source_id
            and payload.source_id in source_scopes
            and (profile.scope is None or profile.scope in source_scopes[payload.source_id])
        ]
        legacy = (
            not backend.source_scopes
            and not backend.acquisition_profiles
            and payload.source_id == catalog.policy.source_id
        )
        try:
            resolved_profiles = [
                resolved
                for profile in profiles
                for _, resolved in [profiled_adapter(default_registry(), profile)]
            ]
        except ValueError:
            raise HTTPException(422, "Configured acquisition profile is invalid") from None
        if payload.profile_fingerprint is not None:
            resolved_profiles = [
                profile
                for profile in resolved_profiles
                if profile.fingerprint == payload.profile_fingerprint
            ]
            legacy = False
        if not legacy and len(resolved_profiles) != 1:
            raise HTTPException(422, "Refresh requires an unambiguous configured source profile")
        return repository.submit_refresh(
            user.user_id,
            {
                "source_id": payload.source_id,
                **({"profile_fingerprint": resolved_profiles[0].fingerprint} if not legacy else {}),
            },
            idempotency_key,
            clock(),
            cooldown_seconds=backend.refresh_cooldown_seconds,
            max_pending=backend.max_pending_per_user,
        )

    def owned_job(user: Principal, job_id: str) -> JobView:
        job = repository.get(user.user_id, job_id)
        if job is None:
            raise HTTPException(404, "Job not found")
        return job

    @app.get("/v1/jobs/{job_id}", response_model=JobView)
    @app.get("/v1/recipes/{job_id}", response_model=JobView)
    def status(job_id: str, user: Annotated[Principal, Depends(principal)]) -> JobView:
        return owned_job(user, job_id)

    @app.get("/v1/jobs/{job_id}/result")
    @app.get("/v1/recipes/{job_id}/result")
    def result(job_id: str, user: Annotated[Principal, Depends(principal)]) -> dict[str, Any]:
        owned_job(user, job_id)
        completed = repository.result(user.user_id, job_id)
        if completed is None:
            raise HTTPException(409, "Job has no successful result yet")
        return completed[0]

    @app.get("/v1/jobs/{job_id}/report", response_class=HTMLResponse)
    def report(job_id: str, user: Annotated[Principal, Depends(principal)]) -> HTMLResponse:
        owned_job(user, job_id)
        completed = repository.result(user.user_id, job_id)
        if completed is None:
            raise HTTPException(409, "Job has no report yet")
        return HTMLResponse(
            completed[1],
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
            },
        )

    @app.delete("/v1/jobs/{job_id}", status_code=204)
    def cancel(job_id: str, user: Annotated[Principal, Depends(principal)]) -> Response:
        owned_job(user, job_id)
        if not repository.cancel(user.user_id, job_id, clock()):
            raise HTTPException(409, "Job already finished")
        return Response(status_code=204)

    if backend.website_enabled:
        from grocery_agent.backend.website import mount_website

        mount_website(app, backend.frontend_dir)

    from fastapi.openapi.utils import get_openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
            schema.setdefault("components", {}).setdefault("securitySchemes", {})[
                "BearerSession"
            ] = {
                "type": "http",
                "scheme": "bearer",
                "description": "Revocable account session token",
            }
            schema["components"]["securitySchemes"]["TransportService"] = {
                "type": "http",
                "scheme": "bearer",
                "description": "Channel-specific transport credential; not a user session",
            }
            for path, operations in schema["paths"].items():
                if path.startswith("/v1/") and path not in PUBLIC_AUTH:
                    for method, operation in operations.items():
                        if method in {"get", "post", "delete", "put", "patch"}:
                            scheme = (
                                "TransportService"
                                if path.startswith("/v1/transports/")
                                else "BearerSession"
                            )
                            operation["security"] = [{scheme: []}]
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]
    return app
