"""Operations queues and summary metrics use the complete case set."""

import os
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

os.environ.setdefault("SESSION_SECRET", "test-only-session-secret-32-characters-long")
os.environ.setdefault("WEBHOOK_TOKEN", "test-only-webhook-token")
os.environ.setdefault("MOCKPAYSWIFT_API_KEY", "test-only-provider-token")

from app import main
from app.database import Base
from app.models import Approval, Conversation, Dispute, Employee, Message, Rider


@pytest_asyncio.fixture
async def dashboard_db(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    monkeypatch.setattr(main, "SessionLocal", sessions)

    async def employee_identity(_request):
        return {"id": "OPS1", "role": "reviewer", "csrf": "test"}, None

    monkeypatch.setattr(main, "_ops_identity", employee_identity)
    base = datetime(2026, 10, 18, tzinfo=UTC)
    async with sessions.begin() as session:
        session.add(
            Rider(
                id="R1",
                display_name="Synthetic Rider",
                phone_identifier="demo:R1",
                city="Fiction City",
                status="ACTIVE",
            )
        )
        session.add(
            Employee(
                id="OPS1",
                display_name="Reviewer",
                role="reviewer",
                password_hash="unused",
                active=True,
            )
        )
        await session.flush()
        session.add(Conversation(id="CV1", rider_id="R1"))
        await session.flush()
        session.add(
            Message(
                message_id="M1",
                rider_id="R1",
                conversation_id="CV1",
                sender="rider",
                text="Check my payout",
                received_at=base,
            )
        )
        await session.flush()
        for index in range(105):
            session.add(
                Dispute(
                    id=f"DONE{index:03}",
                    rider_id="R1",
                    conversation_id="CV1",
                    source_message_id="M1",
                    category="total_payout",
                    status="NO_BALANCE",
                    created_at=base,
                    updated_at=base + timedelta(seconds=1000 + index),
                )
            )
            session.add(
                Dispute(
                    id=f"REVIEW{index:03}",
                    rider_id="R1",
                    conversation_id="CV1",
                    source_message_id="M1",
                    category="total_payout",
                    status="PENDING_APPROVAL",
                    created_at=base,
                    updated_at=base + timedelta(seconds=index),
                )
            )
        for case_id in ("AUTO_PAID", "HUMAN_PAID"):
            session.add(
                Dispute(
                    id=case_id,
                    rider_id="R1",
                    conversation_id="CV1",
                    source_message_id="M1",
                    category="surge",
                    status="PAID",
                    created_at=base,
                    updated_at=base + timedelta(seconds=2000),
                )
            )
        await session.flush()
        session.add(
            Approval(
                dispute_id="HUMAN_PAID",
                employee_id="OPS1",
                revision=1,
                decision="APPROVED",
                reason="Confirmed by an operations reviewer",
            )
        )
    yield sessions
    await engine.dispose()


@pytest.mark.asyncio
async def test_pending_queue_filters_before_limit_and_has_next_page(dashboard_db):
    first = await main.ops_pending(None, page=1)
    second = await main.ops_pending(None, page=2)

    assert len(first["items"]) == 100
    assert first["next_page"] == 2
    assert all(item["status"] == "PENDING_APPROVAL" for item in first["items"])
    assert len(second["items"]) == 5
    assert second["next_page"] is None


@pytest.mark.asyncio
async def test_grouped_filters_and_old_case_detail(dashboard_db):
    escalated = await main._case_rows(status="ESCALATED")
    resolved = await main._case_rows(status="RESOLVED")
    old_case = await main._case_detail("REVIEW000")

    assert len(escalated) == 100
    assert all(item["status"] == "PENDING_APPROVAL" for item in escalated)
    assert len(resolved) == 100
    assert all(item["status"] in {"PAID", "NO_BALANCE", "REJECTED"} for item in resolved)
    assert old_case["id"] == "REVIEW000"


@pytest.mark.asyncio
async def test_auto_resolved_metric_excludes_human_approval(dashboard_db):
    metrics = await main._ops_metrics()

    assert metrics["total_disputes"] == 212
    assert metrics["pending_approvals"] == 105
    assert metrics["auto_resolved"] == 106
