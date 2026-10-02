"""User-owned bindings and separately authenticated transport-facing routes."""

import secrets
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Response
from pydantic import Field

from grocery_agent.contracts import Capabilities, Principal
from grocery_agent.models.common import DomainModel
from grocery_agent.recipes import RecipeRequest

from .channels import ChannelRepository, normalize_address
from .config import BackendSettings
from .repository import AccessDenied, Conflict, ControlRepository, LeaseLost

Channel = Literal["email", "simplex"]


class BindingInput(DomainModel):
    channel: Channel
    address: Annotated[str, Field(min_length=1, max_length=320)]


class EventInput(DomainModel):
    event_id: Annotated[str, Field(min_length=1, max_length=200)]
    address: Annotated[str, Field(min_length=1, max_length=320)]
    text: Annotated[str, Field(min_length=1, max_length=4096)]


class DeliveryAck(DomainModel):
    lease_token: Annotated[str, Field(min_length=20, max_length=200)]
    error: Annotated[str | None, Field(max_length=64)] = None


def channel_enabled(backend: BackendSettings, channel: str) -> bool:
    return bool(
        getattr(backend, f"{channel}_enabled", False)
        and getattr(backend, f"{channel}_transport_token", None)
    )


def authenticate_transport(backend: BackendSettings, path: str, token: str) -> None:
    channel = path.split("/")[3]
    if channel not in {"email", "simplex"} or not channel_enabled(backend, channel):
        raise AccessDenied("transport is disabled")
    expected = getattr(backend, f"{channel}_transport_token").get_secret_value()
    if not secrets.compare_digest(token.encode(), expected.encode()):
        raise AccessDenied("invalid transport credential")


def mount_channels(
    app: FastAPI,
    control: ControlRepository,
    backend: BackendSettings,
    clock: Callable[[], datetime],
    principal: Callable[..., Principal],
    capabilities: Callable[[Principal], Capabilities],
    validate: Callable[[RecipeRequest], RecipeRequest],
) -> None:
    repo = ChannelRepository(control)

    @app.get("/v1/bindings")
    def bindings(user: Annotated[Principal, Depends(principal)]) -> list[dict[str, Any]]:
        return repo.bindings(user.user_id)

    @app.post("/v1/bindings", status_code=202)
    def bind(
        payload: BindingInput, user: Annotated[Principal, Depends(principal)]
    ) -> dict[str, str]:
        if not channel_enabled(backend, payload.channel):
            raise HTTPException(403, "Channel is disabled")
        try:
            address = normalize_address(payload.channel, payload.address)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        binding_id = repo.request_binding(user.user_id, payload.channel, address, clock())
        return {"binding_id": binding_id, "status": "challenge-queued"}

    @app.delete("/v1/bindings/{binding_id}", status_code=204)
    def revoke(binding_id: str, user: Annotated[Principal, Depends(principal)]) -> Response:
        if not repo.revoke(user.user_id, binding_id):
            raise HTTPException(404, "Binding not found")
        return Response(status_code=204)

    def validate_command(request: RecipeRequest) -> RecipeRequest:
        try:
            return validate(request)
        except HTTPException as exc:
            raise ValueError("request not permitted") from exc

    @app.post("/v1/transports/{channel}/events")
    def intake(channel: Channel, payload: EventInput) -> dict[str, Any]:
        try:
            return repo.intake(
                channel,
                payload.event_id,
                payload.address,
                payload.text,
                clock(),
                capabilities(Principal(user_id="transport", username="transport", role="user")),
                validate_command,
                backend.max_pending_per_user,
            )
        except Conflict:
            raise
        except ValueError as exc:
            raise HTTPException(422, "Invalid channel identity or command") from exc

    @app.post("/v1/transports/{channel}/outbox/claim")
    def claim(channel: Channel) -> dict[str, Any] | None:
        return repo.claim(
            channel, clock(), backend.delivery_lease_seconds, backend.delivery_max_attempts
        )

    @app.post("/v1/transports/{channel}/outbox/{delivery_id}/ack", status_code=204)
    def ack(channel: Channel, delivery_id: str, payload: DeliveryAck) -> Response:
        try:
            repo.ack(
                channel,
                delivery_id,
                payload.lease_token,
                clock(),
                payload.error,
                backend.delivery_max_attempts,
            )
        except LeaseLost as exc:
            raise HTTPException(409, "Delivery lease lost") from exc
        return Response(status_code=204)
