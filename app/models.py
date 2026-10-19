"""Operational tables. Provider transactions live in MockPaySwift's separate database."""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def uid() -> str:
    return uuid4().hex


def now() -> datetime:
    return datetime.now(UTC)


AMOUNT = Numeric(14, 2, asdecimal=True)


class Rider(Base):
    __tablename__ = "riders"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(100))
    phone_identifier: Mapped[str] = mapped_column(String(100), unique=True)
    city: Mapped[str] = mapped_column(String(60))
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    password_hash: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Employee(Base):
    __tablename__ = "employees"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(20))
    password_hash: Mapped[str] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Trip(Base):
    __tablename__ = "trips"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    rider_id: Mapped[str] = mapped_column(ForeignKey("riders.id"), index=True)
    trip_date: Mapped[date] = mapped_column(Date, index=True)
    distance_km: Mapped[Decimal] = mapped_column(Numeric(8, 2, asdecimal=True))
    status: Mapped[str] = mapped_column(String(20))
    surge_category: Mapped[str] = mapped_column(String(20))
    base_pay: Mapped[Decimal] = mapped_column(AMOUNT)
    distance_pay: Mapped[Decimal] = mapped_column(AMOUNT)
    surge_pay: Mapped[Decimal] = mapped_column(AMOUNT)
    total_entitlement: Mapped[Decimal] = mapped_column(AMOUNT)
    policy_version: Mapped[str] = mapped_column(String(32))


class EarningPeriod(Base):
    __tablename__ = "earning_periods"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    rider_id: Mapped[str] = mapped_column(ForeignKey("riders.id"), index=True)
    business_date: Mapped[date] = mapped_column(Date, index=True)
    expected_amount: Mapped[Decimal] = mapped_column(AMOUNT)
    policy_version: Mapped[str] = mapped_column(String(32))
    __table_args__ = (
        UniqueConstraint("rider_id", "business_date"),
        CheckConstraint("expected_amount >= 0", name="ck_period_nonnegative"),
    )


class EarningComponent(Base):
    __tablename__ = "earning_components"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    period_id: Mapped[str] = mapped_column(ForeignKey("earning_periods.id"), index=True)
    trip_id: Mapped[str | None] = mapped_column(ForeignKey("trips.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(24))
    amount: Mapped[Decimal] = mapped_column(AMOUNT)
    policy_version: Mapped[str] = mapped_column(String(32))
    __table_args__ = (CheckConstraint("amount >= 0", name="ck_component_nonnegative"),)


class Deduction(Base):
    __tablename__ = "deductions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    period_id: Mapped[str] = mapped_column(ForeignKey("earning_periods.id"), index=True)
    rider_id: Mapped[str] = mapped_column(ForeignKey("riders.id"))
    category: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str] = mapped_column(String(200))
    evidence_reference: Mapped[str] = mapped_column(String(100))
    amount: Mapped[Decimal] = mapped_column(AMOUNT)
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_deduction_positive"),
        CheckConstraint("length(trim(category)) > 0", name="ck_deduction_category"),
        CheckConstraint("length(trim(reason)) > 0", name="ck_deduction_reason"),
        CheckConstraint("length(trim(evidence_reference)) > 0", name="ck_deduction_evidence"),
    )


class PayoutRecord(Base):
    __tablename__ = "payout_records"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    rider_id: Mapped[str] = mapped_column(ForeignKey("riders.id"), index=True)
    period_id: Mapped[str] = mapped_column(ForeignKey("earning_periods.id"), index=True)
    payout_batch_id: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(120), unique=True)
    provider_payment_id: Mapped[str | None] = mapped_column(String(64))
    amount: Mapped[Decimal] = mapped_column(AMOUNT)
    cached_status: Mapped[str] = mapped_column(String(24), default="UNKNOWN")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PayoutAllocation(Base):
    __tablename__ = "payout_allocations"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    payout_record_id: Mapped[str] = mapped_column(ForeignKey("payout_records.id"), index=True)
    component_id: Mapped[str] = mapped_column(ForeignKey("earning_components.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(AMOUNT)
    __table_args__ = (UniqueConstraint("payout_record_id", "component_id"),)


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    rider_id: Mapped[str] = mapped_column(ForeignKey("riders.id"), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Message(Base):
    __tablename__ = "messages"
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_id: Mapped[str] = mapped_column(String(100), unique=True)
    rider_id: Mapped[str] = mapped_column(ForeignKey("riders.id"), index=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    sender: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(Text)
    reply: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    status: Mapped[str] = mapped_column(String(20), default="QUEUED")


class Dispute(Base):
    __tablename__ = "disputes"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    rider_id: Mapped[str] = mapped_column(ForeignKey("riders.id"), index=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"))
    source_message_id: Mapped[str] = mapped_column(ForeignKey("messages.message_id"))
    period_id: Mapped[str | None] = mapped_column(ForeignKey("earning_periods.id"))
    trip_id: Mapped[str | None] = mapped_column(ForeignKey("trips.id"))
    earning_date: Mapped[date | None] = mapped_column(Date)
    category: Mapped[str] = mapped_column(String(32))
    claimed_amount: Mapped[Decimal | None] = mapped_column(AMOUNT)
    verified_outstanding: Mapped[Decimal] = mapped_column(AMOUNT, default=Decimal(0))
    reserved_amount: Mapped[Decimal] = mapped_column(AMOUNT, default=Decimal(0))
    available_to_authorize: Mapped[Decimal] = mapped_column(AMOUNT, default=Decimal(0))
    authorized_amount: Mapped[Decimal] = mapped_column(AMOUNT, default=Decimal(0))
    confirmed_paid_amount: Mapped[Decimal] = mapped_column(AMOUNT, default=Decimal(0))
    status: Mapped[str] = mapped_column(String(32), default="NEW", index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    explanation: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class DisputeRevision(Base):
    __tablename__ = "dispute_revisions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    dispute_id: Mapped[str] = mapped_column(ForeignKey("disputes.id"), index=True)
    message_id: Mapped[str] = mapped_column(ForeignKey("messages.message_id"))
    revision: Mapped[int] = mapped_column(Integer)
    claimed_amount: Mapped[Decimal | None] = mapped_column(AMOUNT)
    trip_id: Mapped[str | None] = mapped_column(String(32))
    category: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint("dispute_id", "revision"),)


class DisputeEvidence(Base):
    __tablename__ = "dispute_evidence"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    dispute_id: Mapped[str] = mapped_column(ForeignKey("disputes.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    reference: Mapped[str] = mapped_column(String(100))
    details: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PaymentAuthorization(Base):
    __tablename__ = "payment_authorizations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    dispute_id: Mapped[str] = mapped_column(ForeignKey("disputes.id"), index=True)
    rider_id: Mapped[str] = mapped_column(ForeignKey("riders.id"), index=True)
    business_date: Mapped[date] = mapped_column(Date)
    mode: Mapped[str] = mapped_column(String(12))
    status: Mapped[str] = mapped_column(String(24), default="RESERVED")
    amount: Mapped[Decimal] = mapped_column(AMOUNT)
    policy_version: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(120), unique=True)
    provider_payment_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    __table_args__ = (
        Index(
            "uq_active_auto_per_rider_day",
            "rider_id",
            "business_date",
            unique=True,
            postgresql_where=text("mode = 'AUTO' AND status NOT IN ('FAILED','CANCELLED')"),
            sqlite_where=text("mode = 'AUTO' AND status NOT IN ('FAILED','CANCELLED')"),
        ),
    )


class EarningAdjustmentAllocation(Base):
    __tablename__ = "earning_adjustment_allocations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    authorization_id: Mapped[str] = mapped_column(
        ForeignKey("payment_authorizations.id"), index=True
    )
    component_id: Mapped[str] = mapped_column(ForeignKey("earning_components.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(AMOUNT)
    status: Mapped[str] = mapped_column(String(20), default="RESERVED")
    __table_args__ = (UniqueConstraint("authorization_id", "component_id"),)


class PaymentAttempt(Base):
    __tablename__ = "payment_attempts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    authorization_id: Mapped[str] = mapped_column(
        ForeignKey("payment_authorizations.id"), index=True
    )
    attempt: Mapped[int] = mapped_column(Integer)
    result: Mapped[str] = mapped_column(String(24))
    error_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (UniqueConstraint("authorization_id", "attempt"),)


class PaymentReconciliation(Base):
    __tablename__ = "payment_reconciliations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    authorization_id: Mapped[str] = mapped_column(
        ForeignKey("payment_authorizations.id"), index=True
    )
    provider_payment_id: Mapped[str | None] = mapped_column(String(64))
    provider_status: Mapped[str] = mapped_column(String(24))
    details: Mapped[dict] = mapped_column(JSON)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    dispute_id: Mapped[str] = mapped_column(ForeignKey("disputes.id"), index=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employees.id"))
    revision: Mapped[int] = mapped_column(Integer)
    decision: Mapped[str] = mapped_column(String(12))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    rider_id: Mapped[str | None] = mapped_column(ForeignKey("riders.id"), index=True)
    dispute_id: Mapped[str | None] = mapped_column(ForeignKey("disputes.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(40))
    actor: Mapped[str] = mapped_column(String(100))
    details: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    kind: Mapped[str] = mapped_column(String(32))
    rider_id: Mapped[str | None] = mapped_column(ForeignKey("riders.id"), index=True)
    source_sequence: Mapped[int | None] = mapped_column(Integer)
    key: Mapped[str] = mapped_column(String(120), unique=True)
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="READY", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=8)
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (
        Index(
            "uq_running_message_per_rider",
            "rider_id",
            unique=True,
            postgresql_where=text("kind = 'MESSAGE' AND status = 'RUNNING'"),
            sqlite_where=text("kind = 'MESSAGE' AND status = 'RUNNING'"),
        ),
    )


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=uid)
    event_type: Mapped[str] = mapped_column(String(40))
    aggregate_id: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
