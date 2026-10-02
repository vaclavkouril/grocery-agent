"""Shared HTTP client for CLI and independently configured channel applications."""

import asyncio
import time
from typing import Any, Self
from urllib.parse import quote

import httpx

from grocery_agent.catalogue.models import CataloguePage, CatalogueQuery
from grocery_agent.contracts import Capabilities, JobPage, JobView, Principal
from grocery_agent.models.offer import Offer
from grocery_agent.models.product import StoreProduct
from grocery_agent.recipes import RecipeRequest


class BackendError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        super().__init__(detail)


def job_path(job_id: str, suffix: str = "") -> str:
    return f"/v1/jobs/{quote(job_id, safe='')}{suffix}"


def decode(response: httpx.Response) -> Any:
    if not 200 <= response.status_code < 300:
        try:
            detail = response.json().get("detail", "Backend request failed")
        except (ValueError, AttributeError):
            detail = "Backend request failed"
        raise BackendError(response.status_code, str(detail))
    return None if response.status_code == 204 else response.json()


class ChannelClient:
    """Service-authenticated transport client; cannot access user catalogue/session routes."""

    def __init__(
        self,
        api_url: str,
        token: str,
        channel: str,
        *,
        timeout: float = 30,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if channel not in {"email", "simplex"}:
            raise ValueError("unsupported transport channel")
        self.path = f"/v1/transports/{channel}"
        self.http = httpx.Client(
            base_url=api_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: Any) -> None:
        self.http.close()

    def intake(self, event_id: str, address: str, text: str) -> dict[str, Any]:
        result = decode(
            self.http.post(
                f"{self.path}/events", json={"event_id": event_id, "address": address, "text": text}
            )
        )
        if not isinstance(result, dict):
            raise BackendError(502, "Invalid intake response")
        return result

    def claim(self) -> dict[str, Any] | None:
        result = decode(self.http.post(f"{self.path}/outbox/claim"))
        if result is not None and not isinstance(result, dict):
            raise BackendError(502, "Invalid outbox response")
        return result

    def ack(self, delivery_id: str, lease_token: str, *, error: str | None = None) -> None:
        decode(
            self.http.post(
                f"{self.path}/outbox/{quote(delivery_id, safe='')}/ack",
                json={"lease_token": lease_token, "error": error},
            )
        )


class AsyncChannelClient:
    def __init__(
        self,
        api_url: str,
        token: str,
        channel: str,
        *,
        timeout: float = 30,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if channel not in {"email", "simplex"}:
            raise ValueError("unsupported transport channel")
        self.path = f"/v1/transports/{channel}"
        self.http = httpx.AsyncClient(
            base_url=api_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.http.aclose()

    async def intake(self, event_id: str, address: str, text: str) -> dict[str, Any]:
        result = decode(
            await self.http.post(
                f"{self.path}/events", json={"event_id": event_id, "address": address, "text": text}
            )
        )
        if not isinstance(result, dict):
            raise BackendError(502, "Invalid intake response")
        return result

    async def claim(self) -> dict[str, Any] | None:
        result = decode(await self.http.post(f"{self.path}/outbox/claim"))
        if result is not None and not isinstance(result, dict):
            raise BackendError(502, "Invalid outbox response")
        return result

    async def ack(self, delivery_id: str, lease_token: str, *, error: str | None = None) -> None:
        decode(
            await self.http.post(
                f"{self.path}/outbox/{quote(delivery_id, safe='')}/ack",
                json={"lease_token": lease_token, "error": error},
            )
        )


def catalogue_page(payload: dict[str, Any]) -> CataloguePage:
    # Wire responses contain computed fields. Rebuild canonical inputs and recompute
    # derived prices rather than relaxing the strict business model's extra-field rule.
    for item in payload["items"]:
        for name in Offer.model_computed_fields:
            item["offer"].pop(name, None)
        for name in StoreProduct.model_computed_fields:
            item["offer"]["product"].pop(name, None)
    return CataloguePage.model_validate(payload)


def offer_params(query: CatalogueQuery) -> list[tuple[str, str]]:
    data = query.model_dump(mode="json", exclude_none=True)
    data["source"] = data.pop("source_id")
    if data.pop("currency") != "CZK":
        raise ValueError("the backend catalogue supports CZK queries")
    params = [
        (key, str(value).lower() if isinstance(value, bool) else str(value))
        for key, value in data.items()
        if key != "retailers"
    ]
    return [*params, *(("retailer", name) for name in query.retailers)]


class GroceryClient:
    def __init__(
        self,
        api_url: str,
        token: str = "",
        *,
        timeout: float = 30,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.http = httpx.Client(
            base_url=api_url,
            headers={"Authorization": f"Bearer {token}"} if token else {},
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: Any) -> None:
        self.http.close()

    def capabilities(self) -> Capabilities:
        return Capabilities.model_validate(decode(self.http.get("/v1/capabilities")))

    def login(self, username: str, password: str) -> None:
        """Establish a bearer session; credentials are never persisted by the client."""
        value = decode(
            self.http.post("/v1/auth/login", json={"username": username, "password": password})
        )
        if not isinstance(value, dict) or not isinstance(value.get("token"), str):
            raise BackendError(502, "Invalid login response")
        self.http.headers["Authorization"] = f"Bearer {value['token']}"

    def refresh(
        self, source_id: str, *, idempotency_key: str, profile_fingerprint: str | None = None
    ) -> JobView:
        payload = {"source_id": source_id}
        if profile_fingerprint:
            payload["profile_fingerprint"] = profile_fingerprint
        return JobView.model_validate(
            decode(
                self.http.post(
                    "/v1/admin/refresh", json=payload, headers={"Idempotency-Key": idempotency_key}
                )
            )
        )

    def me(self) -> Principal:
        return Principal.model_validate(decode(self.http.get("/v1/me")))

    def collections(self, source_id: str | None = None) -> list[dict[str, Any]]:
        value = decode(
            self.http.get("/v1/collections", params={"source": source_id} if source_id else {})
        )
        if not isinstance(value, list):
            raise BackendError(502, "Invalid collections response")
        return value

    def bindings(self) -> list[dict[str, Any]]:
        value = decode(self.http.get("/v1/bindings"))
        if not isinstance(value, list):
            raise BackendError(502, "Invalid bindings response")
        return value

    def bind(self, channel: str, address: str) -> dict[str, Any]:
        value = decode(
            self.http.post("/v1/bindings", json={"channel": channel, "address": address})
        )
        if not isinstance(value, dict):
            raise BackendError(502, "Invalid binding response")
        return value

    def revoke_binding(self, binding_id: str) -> None:
        decode(self.http.delete(f"/v1/bindings/{quote(binding_id, safe='')}"))

    def submit(self, request: RecipeRequest, *, idempotency_key: str | None = None) -> JobView:
        return JobView.model_validate(
            decode(
                self.http.post(
                    "/v1/recipes",
                    json=request.model_dump(mode="json", exclude_unset=True),
                    headers={"Idempotency-Key": idempotency_key or str(request.request_id)},
                )
            )
        )

    def jobs(self, *, offset: int = 0, limit: int = 20) -> JobPage:
        return JobPage.model_validate(
            decode(self.http.get("/v1/jobs", params={"offset": offset, "limit": limit}))
        )

    def job(self, job_id: str) -> JobView:
        return JobView.model_validate(decode(self.http.get(job_path(job_id))))

    def result(self, job_id: str) -> dict[str, Any]:
        value = decode(self.http.get(job_path(job_id, "/result")))
        if not isinstance(value, dict):
            raise BackendError(502, "Invalid job result response")
        return value

    def report(self, job_id: str) -> str:
        response = self.http.get(job_path(job_id, "/report"))
        if not response.is_success:
            decode(response)
        return response.text

    def offers(self, query: CatalogueQuery) -> CataloguePage:
        return catalogue_page(
            decode(self.http.get("/v1/offers", params=tuple(offer_params(query))))
        )

    def cancel(self, job_id: str) -> None:
        decode(self.http.delete(job_path(job_id)))

    def logout(self) -> None:
        decode(self.http.delete("/v1/session"))

    def wait(
        self, job_id: str, *, max_wait_seconds: float = 120, poll_seconds: float = 1
    ) -> JobView:
        if max_wait_seconds <= 0 or poll_seconds <= 0:
            raise ValueError("wait timeout and polling interval must be positive")
        deadline = time.monotonic() + max_wait_seconds
        while True:
            job = self.job(job_id)
            if job.status not in {"queued", "running"}:
                return job
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Job {job_id} is still {job.status}; it remains queued on the server"
                )
            time.sleep(min(poll_seconds, remaining))


class AsyncGroceryClient:
    def __init__(
        self,
        api_url: str,
        token: str = "",
        *,
        timeout: float = 30,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.http = httpx.AsyncClient(
            base_url=api_url,
            headers={"Authorization": f"Bearer {token}"} if token else {},
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
            trust_env=False,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.http.aclose()

    async def capabilities(self) -> Capabilities:
        return Capabilities.model_validate(decode(await self.http.get("/v1/capabilities")))

    async def login(self, username: str, password: str) -> None:
        value = decode(
            await self.http.post(
                "/v1/auth/login", json={"username": username, "password": password}
            )
        )
        if not isinstance(value, dict) or not isinstance(value.get("token"), str):
            raise BackendError(502, "Invalid login response")
        self.http.headers["Authorization"] = f"Bearer {value['token']}"

    async def refresh(
        self, source_id: str, *, idempotency_key: str, profile_fingerprint: str | None = None
    ) -> JobView:
        payload = {"source_id": source_id}
        if profile_fingerprint:
            payload["profile_fingerprint"] = profile_fingerprint
        return JobView.model_validate(
            decode(
                await self.http.post(
                    "/v1/admin/refresh", json=payload, headers={"Idempotency-Key": idempotency_key}
                )
            )
        )

    async def me(self) -> Principal:
        return Principal.model_validate(decode(await self.http.get("/v1/me")))

    async def collections(self, source_id: str | None = None) -> list[dict[str, Any]]:
        value = decode(
            await self.http.get(
                "/v1/collections", params={"source": source_id} if source_id else {}
            )
        )
        if not isinstance(value, list):
            raise BackendError(502, "Invalid collections response")
        return value

    async def bindings(self) -> list[dict[str, Any]]:
        value = decode(await self.http.get("/v1/bindings"))
        if not isinstance(value, list):
            raise BackendError(502, "Invalid bindings response")
        return value

    async def bind(self, channel: str, address: str) -> dict[str, Any]:
        value = decode(
            await self.http.post("/v1/bindings", json={"channel": channel, "address": address})
        )
        if not isinstance(value, dict):
            raise BackendError(502, "Invalid binding response")
        return value

    async def revoke_binding(self, binding_id: str) -> None:
        decode(await self.http.delete(f"/v1/bindings/{quote(binding_id, safe='')}"))

    async def submit(
        self, request: RecipeRequest, *, idempotency_key: str | None = None
    ) -> JobView:
        return JobView.model_validate(
            decode(
                await self.http.post(
                    "/v1/recipes",
                    json=request.model_dump(mode="json", exclude_unset=True),
                    headers={"Idempotency-Key": idempotency_key or str(request.request_id)},
                )
            )
        )

    async def jobs(self, *, offset: int = 0, limit: int = 20) -> JobPage:
        return JobPage.model_validate(
            decode(await self.http.get("/v1/jobs", params={"offset": offset, "limit": limit}))
        )

    async def job(self, job_id: str) -> JobView:
        return JobView.model_validate(decode(await self.http.get(job_path(job_id))))

    async def result(self, job_id: str) -> dict[str, Any]:
        value = decode(await self.http.get(job_path(job_id, "/result")))
        if not isinstance(value, dict):
            raise BackendError(502, "Invalid job result response")
        return value

    async def report(self, job_id: str) -> str:
        response = await self.http.get(job_path(job_id, "/report"))
        if not response.is_success:
            decode(response)
        return response.text

    async def offers(self, query: CatalogueQuery) -> CataloguePage:
        return catalogue_page(
            decode(await self.http.get("/v1/offers", params=tuple(offer_params(query))))
        )

    async def cancel(self, job_id: str) -> None:
        decode(await self.http.delete(job_path(job_id)))

    async def logout(self) -> None:
        decode(await self.http.delete("/v1/session"))

    async def wait(
        self, job_id: str, *, max_wait_seconds: float = 120, poll_seconds: float = 1
    ) -> JobView:
        if max_wait_seconds <= 0 or poll_seconds <= 0:
            raise ValueError("wait timeout and polling interval must be positive")
        deadline = time.monotonic() + max_wait_seconds
        while True:
            job = await self.job(job_id)
            if job.status not in {"queued", "running"}:
                return job
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Job {job_id} is still {job.status}; it remains queued on the server"
                )
            await asyncio.sleep(min(poll_seconds, remaining))
