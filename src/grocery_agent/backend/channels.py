"""Transactional channel intake, proof-of-control and leased delivery.

Only authenticated transport processes supply sender identities. Users cannot verify an
address through their own API session. Recipe commands require a second explicit message.
"""

import secrets
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from grocery_agent.channel_commands import ChannelAction, parse_channel_command
from grocery_agent.contracts import Capabilities
from grocery_agent.localization import Language, channel_help, translate
from grocery_agent.persistence.control.schema import (
    ChannelBindingRow,
    ChannelConfirmationRow,
    ChannelEventRow,
    JobRow,
    OutboxRow,
    UserRow,
)
from grocery_agent.recipes import RecipeRequest

from .repository import AccessDenied, Conflict, ControlRepository, LeaseLost, token_hash


def normalize_address(channel: str, address: str) -> str:
    if channel == "email":
        from grocery_agent.channels.email import normalize_address as email_address
        from grocery_agent.channels.email import valid_address

        normalized = email_address(address)
        if not valid_address(normalized):
            raise ValueError("use a bare email address")
        return normalized
    if channel == "simplex" and address.isascii() and address.isdigit() and int(address) > 0:
        return str(int(address))
    raise ValueError("unsupported channel or contact identity")


class ChannelRepository:
    def __init__(
        self,
        control: ControlRepository,
        settings_reader: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self.control = control
        self.sessions = control.sessions
        if settings_reader is None:
            from .user_state import UserStateRepository

            settings_reader = UserStateRepository(control).settings
        self.settings_reader = settings_reader

    def _languages(self, user_id: str) -> tuple[Language, Language]:
        settings = self.settings_reader(user_id)
        ui = settings.get("ui_language") or "en"
        recipe = settings.get("recipe_language") or ui
        return ("cs" if ui == "cs" else "en", "cs" if recipe == "cs" else "en")

    def request_binding(self, user_id: str, channel: str, address: str, now: datetime) -> str:
        language, _ = self._languages(user_id)
        address = normalize_address(channel, address)
        challenge = secrets.token_urlsafe(32)
        with self.sessions.begin() as session:
            # Lock the account before checking its binding quota or rotating a challenge.
            owner = session.scalar(
                update(UserRow)
                .where(UserRow.id == user_id, UserRow.enabled.is_(True))
                .values(enabled=True)
                .returning(UserRow.id)
            )
            if owner is None:
                raise AccessDenied("account disabled")
            row = session.scalar(
                select(ChannelBindingRow)
                .where(ChannelBindingRow.channel == channel, ChannelBindingRow.address == address)
                .with_for_update()
            )
            if row and row.user_id != user_id:
                raise Conflict("address already belongs to another account")
            if row is None:
                count = len(
                    session.scalars(
                        select(ChannelBindingRow.id).where(
                            ChannelBindingRow.user_id == user_id,
                            ChannelBindingRow.enabled.is_(True),
                        )
                    ).all()
                )
                if count >= 10:
                    raise Conflict("binding limit reached")
                row = ChannelBindingRow(
                    id=str(uuid4()), user_id=user_id, channel=channel, address=address
                )
                session.add(row)
            row.token_hash = token_hash(challenge)
            row.expires_at = now + timedelta(minutes=15)
            row.verified, row.enabled = False, True
            session.flush()
            # Rotation invalidates every queued message for this binding, including old proofs.
            session.execute(
                update(OutboxRow)
                .where(
                    OutboxRow.binding_id == row.id, OutboxRow.state.in_(("pending", "delivering"))
                )
                .values(state="discarded", lease_token=None, lease_until=None, payload=None)
            )
            session.execute(
                update(ChannelConfirmationRow)
                .where(
                    ChannelConfirmationRow.binding_id == row.id,
                    ChannelConfirmationRow.consumed_at.is_(None),
                )
                .values(consumed_at=now)
            )
            self._reply(
                session,
                row.id,
                translate("Verify your grocery account: verify {token}", language, token=challenge),
                now,
                challenge_hash=row.token_hash,
            )
            return row.id

    def bindings(self, user_id: str) -> list[dict[str, Any]]:
        with self.sessions() as session:
            return [
                dict(
                    binding_id=row.id,
                    channel=row.channel,
                    address=row.address,
                    verified=row.verified,
                    enabled=row.enabled,
                )
                for row in session.scalars(
                    select(ChannelBindingRow)
                    .where(ChannelBindingRow.user_id == user_id)
                    .order_by(ChannelBindingRow.channel, ChannelBindingRow.address)
                )
            ]

    def revoke(self, user_id: str, binding_id: str) -> bool:
        with self.sessions.begin() as session:
            found = session.scalar(
                update(ChannelBindingRow)
                .where(ChannelBindingRow.id == binding_id, ChannelBindingRow.user_id == user_id)
                .values(enabled=False)
                .returning(ChannelBindingRow.id)
            )
            if found:
                session.execute(
                    update(OutboxRow)
                    .where(
                        OutboxRow.binding_id == binding_id,
                        OutboxRow.state.in_(("pending", "delivering")),
                    )
                    .values(state="discarded", payload=None, lease_token=None, lease_until=None)
                )
            return found is not None

    @staticmethod
    def _reply(
        session: Session,
        binding_id: str,
        text: str,
        now: datetime,
        *,
        challenge_hash: str | None = None,
    ) -> None:
        session.add(
            OutboxRow(
                id=str(uuid4()),
                job_id=None,
                binding_id=binding_id,
                state="pending",
                attempts=0,
                created_at=now,
                available_at=now,
                payload={"text": text, "challenge_hash": challenge_hash},
            )
        )

    def intake(
        self,
        channel: str,
        event_id: str,
        address: str,
        text: str,
        now: datetime,
        capabilities: Capabilities,
        validate: Callable[[RecipeRequest], RecipeRequest],
        max_pending: int,
    ) -> dict[str, Any]:
        address = normalize_address(channel, address)
        digest = token_hash(f"{address}\n{text}")
        with self.sessions.begin() as session:
            # Lock identity before deduplication, so simultaneous retries publish one response.
            binding = session.scalar(
                update(ChannelBindingRow)
                .where(
                    ChannelBindingRow.channel == channel,
                    ChannelBindingRow.address == address,
                    ChannelBindingRow.enabled.is_(True),
                )
                .values(enabled=True)
                .returning(ChannelBindingRow)
            )
            if binding is None:
                return {"status": "ignored", "reply": None}
            previous = session.get(ChannelEventRow, (channel, event_id))
            if previous:
                if previous.digest != digest:
                    raise Conflict("channel event ID reused with different content")
                return {**previous.response, "status": "duplicate"}
            user = session.get(UserRow, binding.user_id)
            if user is None or not user.enabled:
                return {"status": "ignored", "reply": None}
            reply: str | None
            language, recipe_language = self._languages(binding.user_id)
            try:
                command = parse_channel_command(text, capabilities)
                if isinstance(command, ChannelAction) and command.action == "verify":
                    if (
                        binding.verified
                        or binding.expires_at <= now
                        or not secrets.compare_digest(
                            binding.token_hash, token_hash(command.argument or "")
                        )
                    ):
                        raise AccessDenied("invalid or expired binding challenge")
                    binding.verified = True
                    reply = translate("Channel verified. ", language) + channel_help(language)
                elif not binding.verified:
                    # Do not reflect arbitrary input or create an unsolicited reply loop.
                    reply = None
                elif isinstance(command, RecipeRequest):
                    if "language" not in command.model_fields_set:
                        command = command.model_copy(update={"language": recipe_language})
                    command = validate(command)
                    token = secrets.token_urlsafe(32)
                    session.add(
                        ChannelConfirmationRow(
                            token_hash=token_hash(token),
                            binding_id=binding.id,
                            request=command.model_dump(mode="json"),
                            expires_at=now + timedelta(minutes=15),
                        )
                    )
                    reply = (
                        translate("Recipe parameters: ", language)
                        + command.model_dump_json(exclude={"request_id"})
                        + translate(
                            ". Reply confirm {token} within 15 minutes.", language, token=token
                        )
                    )
                else:
                    reply = self._action(session, binding, command, now, max_pending, language)
            except (ValueError, AccessDenied, Conflict) as exc:
                # Pydantic exceptions may echo secrets/input: do not send their raw text.
                reply = translate(
                    "Command rejected; check help, channel verification and server limits.",
                    language,
                )
                if isinstance(exc, Conflict):
                    reply = translate(
                        "Request could not be queued; check pending jobs and try again.", language
                    )
                if not binding.verified:
                    reply = None
            response = {"status": "processed", "reply": reply}
            session.add(
                ChannelEventRow(
                    channel=channel,
                    event_id=event_id,
                    address=address,
                    digest=digest,
                    response=response,
                    created_at=now,
                )
            )
            if reply:
                self._reply(session, binding.id, reply, now)
            return response

    def _action(
        self,
        session: Session,
        binding: ChannelBindingRow,
        command: ChannelAction,
        now: datetime,
        max_pending: int,
        language: Language = "en",
    ) -> str:
        if command.action == "help":
            return channel_help(language)
        if command.action == "confirm":
            confirmation = session.scalar(
                select(ChannelConfirmationRow)
                .where(
                    ChannelConfirmationRow.token_hash == token_hash(command.argument or ""),
                    ChannelConfirmationRow.binding_id == binding.id,
                )
                .with_for_update()
            )
            if confirmation is None or confirmation.expires_at <= now:
                raise AccessDenied("invalid confirmation")
            if confirmation.consumed_at is not None:
                if confirmation.job_id:
                    return translate(
                        "Already accepted job {job}.", language, job=confirmation.job_id
                    )
                raise AccessDenied("invalidated confirmation")
            key = f"channel:{confirmation.token_hash}"
            payload = {
                **confirmation.request,
                "request_id": str(uuid5(NAMESPACE_URL, f"{binding.user_id}/{key}")),
            }
            job = self.control.submit(
                binding.user_id,
                payload,
                key,
                now,
                binding_id=binding.id,
                max_pending=max_pending,
                transaction=session,
            )
            confirmation.consumed_at, confirmation.job_id = now, job.job_id
            return translate(
                "Accepted job {job}. Use status {job} to check progress.", language, job=job.job_id
            )
        job = session.scalar(
            select(JobRow).where(JobRow.id == command.argument, JobRow.user_id == binding.user_id)
        )
        if job is None:
            return translate("Job not found.", language)
        if command.action == "status":
            return translate(
                "Job {job}: {state}, {phase}.",
                language,
                job=job.id,
                state=translate(job.state, language),
                phase=translate(job.phase, language),
            )
        if command.action == "cancel":
            cancelled = session.scalar(
                update(JobRow)
                .where(
                    JobRow.id == job.id,
                    JobRow.user_id == binding.user_id,
                    JobRow.state.in_(("queued", "running")),
                )
                .values(
                    state="cancelled",
                    phase="cancelled",
                    updated_at=now,
                    lease_token=None,
                    lease_until=None,
                )
                .returning(JobRow)
            )
            if cancelled:
                self.control._notify(session, cancelled, now)
                return translate("Cancelled job {job}.", language, job=job.id)
            return translate("Job already finished.", language)
        raise ValueError("unsupported action")

    def claim(
        self, channel: str, now: datetime, lease_seconds: int, max_attempts: int
    ) -> dict[str, Any] | None:
        with self.sessions.begin() as session:
            eligible = or_(
                OutboxRow.state == "pending",
                (OutboxRow.state == "delivering") & (OutboxRow.lease_until <= now),
            )
            # Pending terminal entries survive a process restart. Expired leases are retried.
            channel_ids = select(ChannelBindingRow.id).where(ChannelBindingRow.channel == channel)
            session.execute(
                update(OutboxRow)
                .where(
                    OutboxRow.binding_id.in_(channel_ids),
                    eligible,
                    OutboxRow.attempts >= max_attempts,
                )
                .values(
                    state="failed",
                    error_code="attempts_exhausted",
                    payload=None,
                    lease_token=None,
                    lease_until=None,
                )
            )
            for _ in range(100):
                next_id = (
                    select(OutboxRow.id)
                    .where(
                        OutboxRow.binding_id.in_(channel_ids),
                        eligible,
                        OutboxRow.attempts < max_attempts,
                        or_(OutboxRow.available_at.is_(None), OutboxRow.available_at <= now),
                    )
                    .order_by(OutboxRow.created_at, OutboxRow.id)
                    .limit(1)
                    .scalar_subquery()
                )
                lease = secrets.token_hex(16)
                row = session.scalar(
                    update(OutboxRow)
                    .where(OutboxRow.id == next_id, eligible)
                    .values(
                        state="delivering",
                        attempts=OutboxRow.attempts + 1,
                        lease_token=lease,
                        lease_until=now + timedelta(seconds=lease_seconds),
                    )
                    .returning(OutboxRow)
                )
                if row is None:
                    return None
                binding = session.get(ChannelBindingRow, row.binding_id)
                user = session.get(UserRow, binding.user_id) if binding else None
                payload = row.payload or {}
                challenge = payload.get("challenge_hash")
                permitted = bool(
                    binding
                    and binding.enabled
                    and user
                    and user.enabled
                    and (
                        (
                            challenge
                            and challenge == binding.token_hash
                            and binding.expires_at > now
                            and not binding.verified
                        )
                        or (not challenge and binding.verified)
                    )
                )
                if not permitted:
                    row.state, row.payload, row.lease_token, row.lease_until = (
                        "discarded",
                        None,
                        None,
                        None,
                    )
                    session.flush()
                    continue
                assert binding is not None
                job = session.get(JobRow, row.job_id) if row.job_id else None
                if row.job_id and (job is None or job.user_id != binding.user_id):
                    row.state, row.payload = "discarded", None
                    session.flush()
                    continue
                language, _ = self._languages(binding.user_id)
                text = payload.get(
                    "text",
                    translate(
                        "Job {job}: {state}.",
                        language,
                        job=job.id,
                        state=translate(job.state, language),
                    )
                    if job
                    else "",
                )
                return {
                    "delivery_id": row.id,
                    "lease_token": lease,
                    "address": binding.address,
                    "text": text,
                    "subject": translate(
                        "Grocery Agent recipe" if job else "Grocery Agent command", language
                    ),
                    "report_html": job.report_html if job and job.state == "succeeded" else None,
                    "result": job.result if job and job.state == "succeeded" else None,
                }
            return None

    def ack(
        self,
        channel: str,
        delivery_id: str,
        lease_token: str,
        now: datetime,
        error: str | None,
        max_attempts: int,
    ) -> None:
        with self.sessions.begin() as session:
            row = session.scalar(
                select(OutboxRow)
                .join(ChannelBindingRow)
                .where(
                    OutboxRow.id == delivery_id,
                    ChannelBindingRow.channel == channel,
                    OutboxRow.state == "delivering",
                    OutboxRow.lease_token == lease_token,
                    OutboxRow.lease_until > now,
                )
                .with_for_update()
            )
            if row is None:
                raise LeaseLost("delivery lease lost")
            if error is None:
                row.state, row.payload, row.error_code = "sent", None, None
            elif row.attempts >= max_attempts:
                row.state, row.payload, row.error_code = "failed", None, "transport_failed"
            else:
                row.state, row.error_code = "pending", "transport_failed"
                row.available_at = now + timedelta(seconds=min(300, 2**row.attempts))
            row.lease_token, row.lease_until = None, None
