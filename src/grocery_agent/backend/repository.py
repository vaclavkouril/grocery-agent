"""Short control transactions with atomic job claims and fenced completion."""

import hashlib
import secrets
from contextlib import nullcontext
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import Engine, case, func, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from grocery_agent.contracts import JobPage
from grocery_agent.contracts import JobView as JobView
from grocery_agent.contracts import Principal as Principal
from grocery_agent.models.common import DomainModel
from grocery_agent.persistence.control.schema import (
    AuthRateRow,
    ChannelBindingRow,
    InvitationRow,
    JobRow,
    OutboxRow,
    RefreshGateRow,
    RefreshReceiptRow,
    SessionRow,
    UserRow,
)

from .passwords import hash_password, verify_password


class AccessDenied(ValueError):
    pass


class Conflict(ValueError):
    pass


class LeaseLost(ValueError):
    pass


class RateLimited(ValueError):
    pass


class ClaimedJob(DomainModel):
    job_id: str
    user_id: str
    kind: str
    request: dict[str, Any]
    inputs: dict[str, Any] | None
    lease_token: str


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def view(row: JobRow) -> JobView:
    return JobView(
        job_id=row.id,
        kind=row.kind,
        status=row.state,
        phase=row.phase,
        attempts=row.attempts,
        created_at=row.created_at,
        updated_at=row.updated_at,
        error_code=row.error_code,
    )


class ControlRepository:
    def __init__(self, engine: Engine) -> None:
        self.sessions = sessionmaker(engine, expire_on_commit=False)

    def _insert(self, session: Session, row: Any) -> Any:
        if session.get_bind().dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            return pg_insert(row)
        else:
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert

            return sqlite_insert(row)

    def check_auth_rate(
        self,
        action: str,
        client: str,
        identity: str,
        now: datetime,
        *,
        limit: int = 10,
        window_seconds: int = 300,
    ) -> None:
        """Fixed hash buckets bound attacker-controlled storage to 1026 rows.

        Global, client and identity budgets commit before any credential checks.
        Bucket collisions fail closed; no supplied usernames/IPs are persisted.
        """
        if action not in {"login", "invite"} or not 1 <= limit <= 100:
            raise ValueError("invalid authentication throttle configuration")
        keys = [(f"{action}:global", limit * 10)]
        for kind, value in (("client", client), ("identity", identity)):
            bucket = int(token_hash(value)[:8], 16) % 256
            keys.append((f"{action}:{kind}:{bucket}", limit))
        blocked = False
        cutoff = now - timedelta(seconds=window_seconds)
        with self.sessions.begin() as session:
            for key, budget in keys:
                insert = self._insert(session, AuthRateRow).values(
                    bucket=key, window_start=now, attempts=1
                )
                reset = AuthRateRow.window_start <= cutoff
                attempts = session.scalar(
                    insert.on_conflict_do_update(
                        index_elements=[AuthRateRow.bucket],
                        set_={
                            "window_start": case((reset, now), else_=AuthRateRow.window_start),
                            "attempts": case(
                                (reset, 1), else_=func.min(AuthRateRow.attempts + 1, 10001)
                            )
                            if session.get_bind().dialect.name == "sqlite"
                            else case(
                                (reset, 1), else_=func.least(AuthRateRow.attempts + 1, 10001)
                            ),
                        },
                    ).returning(AuthRateRow.attempts)
                )
                blocked = blocked or bool(attempts and attempts > budget)
        if blocked:
            raise RateLimited("Authentication temporarily unavailable")

    def set_csrf(self, token: str, csrf_token: str) -> None:
        with self.sessions.begin() as session:
            session.execute(
                update(SessionRow)
                .where(SessionRow.token_hash == token_hash(token), SessionRow.revoked.is_(False))
                .values(csrf_digest=token_hash(csrf_token))
            )

    def validate_csrf(self, token: str, csrf_token: str) -> bool:
        if not 20 <= len(csrf_token) <= 200:
            return False
        with self.sessions() as session:
            digest = session.scalar(
                select(SessionRow.csrf_digest).where(
                    SessionRow.token_hash == token_hash(token), SessionRow.revoked.is_(False)
                )
            )
            return bool(digest and secrets.compare_digest(digest, token_hash(csrf_token)))

    def login(self, username: str, password: str, now: datetime, hours: int) -> str:
        with self.sessions() as session:
            user = session.scalar(select(UserRow).where(UserRow.username == username))
            encoded = user.password_hash if user else None
            user_id = user.id if user else None
        valid = verify_password(password, encoded)
        if not valid or user_id is None:
            raise AccessDenied("Invalid credentials")
        token = secrets.token_urlsafe(32)
        with self.sessions.begin() as session:
            # Serialize against disabling the account or replacing its password.
            enabled = session.scalar(
                update(UserRow)
                .where(
                    UserRow.id == user_id,
                    UserRow.enabled.is_(True),
                    UserRow.password_hash == encoded,
                )
                .values(enabled=True)
                .returning(UserRow.id)
            )
            if enabled is None:
                raise AccessDenied("Invalid credentials")
            session.add(
                SessionRow(
                    token_hash=token_hash(token),
                    user_id=user_id,
                    expires_at=now + timedelta(hours=hours),
                    revoked=False,
                )
            )
        return token

    def bootstrap(
        self, username: str, now: datetime, hours: int = 24, *, password: str | None = None
    ) -> str:
        encoded = hash_password(password) if password is not None else None
        token = secrets.token_urlsafe(32)
        with self.sessions.begin() as session:
            # Bootstrap is an explicit local management operation, never an HTTP route.
            user = session.scalar(select(UserRow).where(UserRow.username == username))
            if user is None:
                user = UserRow(
                    id=str(uuid4()), username=username, role="admin", enabled=True, created_at=now
                )
                session.add(user)
                session.flush()
            elif user.role != "admin" or not user.enabled:
                raise AccessDenied("bootstrap requires an enabled administrator")
            if encoded is not None:
                user.password_hash = encoded
            session.add(
                SessionRow(
                    token_hash=token_hash(token),
                    user_id=user.id,
                    expires_at=now + timedelta(hours=hours),
                    revoked=False,
                )
            )
        return token

    def authenticate(self, token: str, now: datetime) -> Principal:
        with self.sessions() as session:
            user = session.scalar(
                select(UserRow)
                .join(SessionRow)
                .where(
                    SessionRow.token_hash == token_hash(token),
                    SessionRow.expires_at > now,
                    SessionRow.revoked.is_(False),
                    UserRow.enabled.is_(True),
                )
            )
            if user is None:
                raise AccessDenied("invalid or expired session")
            return Principal(user_id=user.id, username=user.username, role=user.role)

    def revoke(self, token: str) -> None:
        with self.sessions.begin() as session:
            session.execute(
                update(SessionRow)
                .where(SessionRow.token_hash == token_hash(token))
                .values(revoked=True)
            )

    def invite(self, username: str, now: datetime) -> str:
        token = secrets.token_urlsafe(32)
        with self.sessions.begin() as session:
            session.add(
                InvitationRow(
                    token_hash=token_hash(token),
                    username=username,
                    expires_at=now + timedelta(hours=24),
                )
            )
        return token

    def accept_invite(
        self, token: str, now: datetime, hours: int, *, password: str | None = None
    ) -> str:
        encoded = hash_password(password) if password is not None else None
        new_token = secrets.token_urlsafe(32)
        with self.sessions.begin() as session:
            username = session.scalar(
                update(InvitationRow)
                .where(
                    InvitationRow.token_hash == token_hash(token),
                    InvitationRow.accepted_at.is_(None),
                    InvitationRow.expires_at > now,
                )
                .values(accepted_at=now)
                .returning(InvitationRow.username)
            )
            if username is None:
                raise AccessDenied("invalid or expired invitation")
            if session.scalar(select(UserRow.id).where(UserRow.username == username)):
                raise AccessDenied("invalid or expired invitation")
            user_id = str(uuid4())
            session.add(
                UserRow(
                    id=user_id,
                    username=username,
                    role="user",
                    enabled=True,
                    created_at=now,
                    password_hash=encoded,
                )
            )
            session.flush()
            session.add(
                SessionRow(
                    token_hash=token_hash(new_token),
                    user_id=user_id,
                    expires_at=now + timedelta(hours=hours),
                    revoked=False,
                )
            )
        return new_token

    def submit_refresh(
        self,
        user_id: str,
        payload: dict[str, Any],
        key: str,
        now: datetime,
        *,
        cooldown_seconds: int = 300,
        max_pending: int = 10,
    ) -> JobView:
        identity = token_hash(
            f"{payload['source_id']}/{payload.get('profile_fingerprint', 'legacy')}"
        )
        with self.sessions.begin() as session:
            admin = session.scalar(
                update(UserRow)
                .where(UserRow.id == user_id, UserRow.enabled.is_(True), UserRow.role == "admin")
                .values(enabled=True)
                .returning(UserRow.id)
            )
            if admin is None:
                raise AccessDenied("Administrator required")
            receipt = session.get(RefreshReceiptRow, (user_id, key))
            if receipt is not None:
                row = session.get(JobRow, receipt.job_id)
                if receipt.request != payload or row is None or row.user_id != user_id:
                    raise Conflict("idempotency key already used for a different request")
                return view(row)
            # Atomic upsert takes the source/profile lock, including its first creation.
            insert = self._insert(session, RefreshGateRow).values(
                identity=identity, cooldown_until=now
            )
            session.execute(
                insert.on_conflict_do_update(
                    index_elements=[RefreshGateRow.identity], set_={"identity": identity}
                )
            )
            gate = session.get(RefreshGateRow, identity)
            assert gate is not None
            previous = session.get(JobRow, gate.job_id) if gate.job_id else None
            held = previous is not None and (
                previous.state in {"queued", "running"} or gate.cooldown_until > now
            )
            if held:
                assert previous is not None
                if previous.user_id != user_id:
                    raise Conflict("Refresh temporarily unavailable for this source profile")
                if previous.request != payload:
                    raise Conflict("Refresh profile identity changed")
                job = view(previous)
                # Reserve every idempotency key, including keys used to coalesce work.
                other = session.scalar(
                    select(JobRow.id).where(
                        JobRow.user_id == user_id, JobRow.idempotency_key == key
                    )
                )
                if other is not None and other != previous.id:
                    raise Conflict("idempotency key already used for a different request")
            else:
                job = self.submit(
                    user_id,
                    payload,
                    key,
                    now,
                    kind="refresh",
                    max_pending=max_pending,
                    transaction=session,
                )
                gate.job_id = job.job_id
                gate.cooldown_until = now + timedelta(seconds=cooldown_seconds)
            session.add(
                RefreshReceiptRow(
                    user_id=user_id, idempotency_key=key, job_id=job.job_id, request=payload
                )
            )
            return job

    def create_binding(self, user_id: str, channel: str, address: str, now: datetime) -> str:
        """Issue a challenge for the transport to deliver; never marks it verified."""
        token = secrets.token_urlsafe(32)
        with self.sessions.begin() as session:
            session.add(
                ChannelBindingRow(
                    id=str(uuid4()),
                    user_id=user_id,
                    channel=channel,
                    address=address,
                    token_hash=token_hash(token),
                    expires_at=now + timedelta(minutes=15),
                    verified=False,
                    enabled=True,
                )
            )
        return token

    def verify_binding(self, channel: str, address: str, token: str, now: datetime) -> str:
        with self.sessions.begin() as session:
            binding_id = session.scalar(
                update(ChannelBindingRow)
                .where(
                    ChannelBindingRow.channel == channel,
                    ChannelBindingRow.address == address,
                    ChannelBindingRow.token_hash == token_hash(token),
                    ChannelBindingRow.expires_at > now,
                    ChannelBindingRow.verified.is_(False),
                    ChannelBindingRow.enabled.is_(True),
                )
                .values(verified=True)
                .returning(ChannelBindingRow.id)
            )
            if binding_id is None:
                raise AccessDenied("invalid channel challenge")
            return binding_id

    def submit(
        self,
        user_id: str,
        payload: dict[str, Any],
        key: str,
        now: datetime,
        *,
        kind: str = "recipe",
        binding_id: str | None = None,
        max_pending: int = 10,
        transaction: Session | None = None,
    ) -> JobView:
        with (
            nullcontext(transaction)
            if transaction is not None
            else self.sessions.begin() as session
        ):
            # Acquire the user/write lock before deduplication and quota reads on SQLite.
            user = session.scalar(
                update(UserRow)
                .where(UserRow.id == user_id, UserRow.enabled.is_(True))
                .values(enabled=True)
                .returning(UserRow.id)
            )
            if user is None:
                raise AccessDenied("account disabled")
            previous = session.scalar(
                select(JobRow).where(JobRow.user_id == user_id, JobRow.idempotency_key == key)
            )
            refresh_receipt = session.get(RefreshReceiptRow, (user_id, key))
            if refresh_receipt is not None and (
                kind != "refresh" or refresh_receipt.request != payload
            ):
                raise Conflict("idempotency key already used for a different request")
            if previous:
                if (previous.request, previous.kind, previous.binding_id) != (
                    payload,
                    kind,
                    binding_id,
                ):
                    raise Conflict("idempotency key already used for a different request")
                return view(previous)
            if binding_id:
                binding = session.scalar(
                    select(ChannelBindingRow).where(
                        ChannelBindingRow.id == binding_id,
                        ChannelBindingRow.user_id == user_id,
                        ChannelBindingRow.verified.is_(True),
                        ChannelBindingRow.enabled.is_(True),
                    )
                )
                if binding is None:
                    raise AccessDenied("channel is unavailable or unverified")
            pending = (
                session.scalar(
                    select(func.count())
                    .select_from(JobRow)
                    .where(JobRow.user_id == user_id, JobRow.state.in_(("queued", "running")))
                )
                or 0
            )
            if pending >= max_pending:
                raise Conflict("pending job limit reached")
            row = JobRow(
                id=str(uuid4()),
                user_id=user_id,
                idempotency_key=key,
                kind=kind,
                request=payload,
                binding_id=binding_id,
                state="queued",
                phase="queued",
                attempts=0,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return view(row)

    def get(self, user_id: str, job_id: str) -> JobView | None:
        with self.sessions() as session:
            row = session.scalar(
                select(JobRow).where(JobRow.id == job_id, JobRow.user_id == user_id)
            )
            return view(row) if row else None

    def list_jobs(self, user_id: str, *, offset: int = 0, limit: int = 20) -> JobPage:
        if not 0 <= offset or not 1 <= limit <= 100:
            raise ValueError("invalid job pagination")
        with self.sessions() as session:
            predicate = JobRow.user_id == user_id
            total = session.scalar(select(func.count()).select_from(JobRow).where(predicate)) or 0
            rows = session.scalars(
                select(JobRow)
                .where(predicate)
                .order_by(JobRow.created_at.desc(), JobRow.id.desc())
                .offset(offset)
                .limit(limit)
            )
            return JobPage(
                items=tuple(view(row) for row in rows), total=total, offset=offset, limit=limit
            )

    def result(self, user_id: str, job_id: str) -> tuple[dict[str, Any], str] | None:
        with self.sessions() as session:
            row = session.scalar(
                select(JobRow).where(
                    JobRow.id == job_id, JobRow.user_id == user_id, JobRow.state == "succeeded"
                )
            )
            return (row.result or {}, row.report_html or "") if row else None

    def cancel(self, user_id: str, job_id: str, now: datetime) -> bool:
        with self.sessions.begin() as session:
            row = session.scalar(
                update(JobRow)
                .where(
                    JobRow.id == job_id,
                    JobRow.user_id == user_id,
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
            if row is None:
                return False
            self._notify(session, row, now)
            return True

    def claim(self, now: datetime, lease_seconds: int, max_attempts: int) -> ClaimedJob | None:
        with self.sessions.begin() as session:
            # Exhausted leases become terminal before looking for eligible work.
            exhausted = session.scalars(
                update(JobRow)
                .where(
                    JobRow.state == "running",
                    JobRow.lease_until <= now,
                    JobRow.attempts >= max_attempts,
                )
                .values(
                    state="failed",
                    phase="failed",
                    error_code="attempts_exhausted",
                    lease_token=None,
                    lease_until=None,
                    updated_at=now,
                )
                .returning(JobRow)
            )
            for exhausted_row in exhausted:
                self._notify(session, exhausted_row, now)
            eligible = or_(
                JobRow.state == "queued", (JobRow.state == "running") & (JobRow.lease_until <= now)
            )
            candidates = select(JobRow.id).where(eligible, JobRow.attempts < max_attempts)
            next_id = candidates.order_by(JobRow.created_at, JobRow.id).limit(1).scalar_subquery()
            lease = secrets.token_hex(16)
            row = session.scalar(
                update(JobRow)
                .where(JobRow.id == next_id, eligible, JobRow.attempts < max_attempts)
                .values(
                    state="running",
                    phase="preparing",
                    attempts=JobRow.attempts + 1,
                    lease_token=lease,
                    lease_until=now + timedelta(seconds=lease_seconds),
                    updated_at=now,
                )
                .returning(JobRow)
            )
            if row is None:
                return None
            return ClaimedJob(
                job_id=row.id,
                user_id=row.user_id,
                kind=row.kind,
                request=row.request,
                inputs=row.inputs,
                lease_token=lease,
            )

    def _fence(self, job: ClaimedJob, now: datetime) -> Any:
        return (
            (JobRow.id == job.job_id)
            & (JobRow.state == "running")
            & (JobRow.lease_token == job.lease_token)
            & (JobRow.lease_until > now)
        )

    def heartbeat(self, job: ClaimedJob, now: datetime, lease_seconds: int) -> bool:
        with self.sessions.begin() as session:
            return (
                session.scalar(
                    update(JobRow)
                    .where(self._fence(job, now))
                    .values(lease_until=now + timedelta(seconds=lease_seconds), updated_at=now)
                    .returning(JobRow.id)
                )
                is not None
            )

    def authorized(self, job: ClaimedJob) -> bool:
        with self.sessions() as session:
            row = session.get(JobRow, job.job_id)
            user = session.get(UserRow, job.user_id)
            if row is None or user is None or not user.enabled:
                return False
            if row.kind == "refresh" and user.role != "admin":
                return False
            if row.binding_id:
                binding = session.get(ChannelBindingRow, row.binding_id)
                return bool(
                    binding and binding.enabled and binding.verified and binding.user_id == user.id
                )
            return True

    def pin(self, job: ClaimedJob, inputs: dict[str, Any], now: datetime) -> None:
        with self.sessions.begin() as session:
            changed = session.scalar(
                update(JobRow)
                .where(self._fence(job, now), JobRow.inputs.is_(None))
                .values(inputs=inputs, phase="generating", updated_at=now)
                .returning(JobRow.id)
            )
            if changed is None:
                raise LeaseLost("job lease lost or inputs already pinned")

    def finish(
        self,
        job: ClaimedJob,
        now: datetime,
        *,
        result: dict[str, Any] | None = None,
        html: str = "",
        error: str | None = None,
    ) -> None:
        state = "failed" if error else "succeeded"
        with self.sessions.begin() as session:
            row = session.scalar(
                update(JobRow)
                .where(self._fence(job, now))
                .values(
                    state=state,
                    phase=state,
                    result=result,
                    report_html=html,
                    error_code=error,
                    lease_token=None,
                    lease_until=None,
                    updated_at=now,
                )
                .returning(JobRow)
            )
            if row is None:
                raise LeaseLost("stale worker cannot complete this job")
            self._notify(session, row, now)

    def _notify(self, session: Session, row: JobRow, now: datetime) -> None:
        if row.binding_id:
            binding = session.get(ChannelBindingRow, row.binding_id)
            user = session.get(UserRow, row.user_id)
            if (
                binding
                and binding.enabled
                and binding.verified
                and user
                and user.enabled
                and binding.user_id == row.user_id
            ):
                session.add(
                    OutboxRow(
                        id=str(uuid4()),
                        job_id=row.id,
                        binding_id=row.binding_id,
                        state="pending",
                        attempts=0,
                        created_at=now,
                    )
                )
