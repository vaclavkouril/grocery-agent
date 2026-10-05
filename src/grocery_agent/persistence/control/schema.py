from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from grocery_agent.persistence.types import UTCDateTime


class ControlBase(DeclarativeBase):
    pass


class UserRow(ControlBase):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str | None] = mapped_column(String(512))
    role: Mapped[str] = mapped_column(String(24), default="user")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class ProfileRow(ControlBase):
    __tablename__ = "user_profiles"
    __table_args__ = (UniqueConstraint("user_id", "name"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class AccountStateRow(ControlBase):
    __tablename__ = "account_state"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    settings_revision: Mapped[int] = mapped_column(Integer, default=0)
    ui_language: Mapped[str | None] = mapped_column(String(2))
    recipe_language: Mapped[str | None] = mapped_column(String(2))
    pantry_revision: Mapped[int] = mapped_column(Integer, default=0)
    pantry_items: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class SessionRow(ControlBase):
    __tablename__ = "sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    csrf_digest: Mapped[str | None] = mapped_column(String(64))


class AuthRateRow(ControlBase):
    __tablename__ = "auth_rate_limits"
    bucket: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(UTCDateTime)
    attempts: Mapped[int] = mapped_column(Integer)


class RefreshGateRow(ControlBase):
    __tablename__ = "refresh_gates"
    identity: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"))
    cooldown_until: Mapped[datetime] = mapped_column(UTCDateTime)


class RefreshReceiptRow(ControlBase):
    __tablename__ = "refresh_receipts"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))
    request: Mapped[dict[str, Any]] = mapped_column(JSON)


class InvitationRow(ControlBase):
    __tablename__ = "invitations"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    accepted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class ChannelBindingRow(ControlBase):
    __tablename__ = "channel_bindings"
    __table_args__ = (UniqueConstraint("channel", "address"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    channel: Mapped[str] = mapped_column(String(24))
    address: Mapped[str] = mapped_column(String(320))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class JobRow(ControlBase):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("user_id", "idempotency_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(24))
    request: Mapped[dict[str, Any]] = mapped_column(JSON)
    binding_id: Mapped[str | None] = mapped_column(ForeignKey("channel_bindings.id"))
    state: Mapped[str] = mapped_column(String(24), index=True)
    phase: Mapped[str] = mapped_column(String(40))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    inputs: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    report_html: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(64))


class OutboxRow(ControlBase):
    __tablename__ = "notification_outbox"
    __table_args__ = (UniqueConstraint("job_id", "binding_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"))
    binding_id: Mapped[str] = mapped_column(ForeignKey("channel_bindings.id"))
    state: Mapped[str] = mapped_column(String(24), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    available_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    error_code: Mapped[str | None] = mapped_column(String(64))


class ChannelEventRow(ControlBase):
    __tablename__ = "channel_events"
    channel: Mapped[str] = mapped_column(String(24), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    address: Mapped[str] = mapped_column(String(320))
    digest: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class ChannelConfirmationRow(ControlBase):
    __tablename__ = "channel_confirmations"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    binding_id: Mapped[str] = mapped_column(ForeignKey("channel_bindings.id"), index=True)
    request: Mapped[dict[str, Any]] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"))
