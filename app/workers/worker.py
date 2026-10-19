import asyncio
import logging
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import aliased

from app.config import get_settings
from app.database import SessionLocal
from app.integrations.mockpayswift_client import MockPaySwiftClient
from app.models import AuditEvent, Dispute, Job, Message, OutboxEvent, PaymentAuthorization
from app.services.messages import process_message
from app.services.payments import execute_payment, reconcile_existing, transition

log = logging.getLogger("quickdrop.worker")


async def claim_job() -> Job | None:
    current = datetime.now(UTC)
    earlier = aliased(Job)
    async with SessionLocal.begin() as session:
        query = (
            select(Job)
            .where(
                or_(
                    and_(Job.status == "READY", Job.run_at <= current),
                    and_(Job.status == "RUNNING", Job.locked_until < current),
                ),
                or_(
                    Job.kind != "MESSAGE",
                    ~exists(
                        select(earlier.id).where(
                            earlier.kind == "MESSAGE",
                            earlier.rider_id == Job.rider_id,
                            earlier.source_sequence < Job.source_sequence,
                            earlier.status.not_in(["DONE", "DEAD"]),
                        )
                    ),
                ),
            )
            .order_by(Job.run_at, Job.source_sequence, Job.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        job = await session.scalar(query)
        if job is None:
            return None
        job.status = "RUNNING"
        job.attempts += 1
        job.locked_until = current + timedelta(seconds=90)
        await session.flush()
        session.expunge(job)
        return job


async def finish_job(job_id: str, result: str) -> None:
    async with SessionLocal.begin() as session:
        job = await session.get(Job, job_id, with_for_update=True)
        if job is None:
            return
        if result == "RETRY":
            if job.attempts >= job.max_attempts:
                job.status = "DEAD"
                job.last_error = "retry limit reached"
                if job.kind == "PAYMENT":
                    auth = await session.get(PaymentAuthorization, job.payload["authorization_id"])
                    if auth and auth.status not in {"CONFIRMED", "FAILED"}:
                        transition(auth, "OPS_REVIEW")
                        dispute = await session.get(Dispute, auth.dispute_id)
                        dispute.status = "RECONCILIATION_REVIEW"
                        session.add(
                            AuditEvent(
                                rider_id=auth.rider_id,
                                dispute_id=auth.dispute_id,
                                event_type="RECONCILIATION_REVIEW",
                                actor="worker",
                                details={"authorization_id": auth.id},
                            )
                        )
                elif job.kind == "MESSAGE":
                    message = await session.scalar(
                        select(Message).where(Message.message_id == job.payload["message_id"])
                    )
                    if message and message.status != "PROCESSED":
                        message.status = "PROCESSED"
                        message.reply = (
                            "Your message needs employee review. No correction was authorized."
                        )
                        session.add(
                            Dispute(
                                rider_id=message.rider_id,
                                conversation_id=message.conversation_id,
                                source_message_id=message.message_id,
                                category="other",
                                status="UNDER_REVIEW",
                                explanation="Worker retry limit reached.",
                            )
                        )
                return
            job.status = "READY"
            job.run_at = datetime.now(UTC) + timedelta(seconds=min(300, 2 ** min(job.attempts, 8)))
        else:
            job.status = "DONE"
            if job.kind == "PAYMENT":
                for event in (
                    await session.scalars(
                        select(OutboxEvent).where(
                            OutboxEvent.aggregate_id == job.payload["authorization_id"],
                            OutboxEvent.delivered_at.is_(None),
                        )
                    )
                ).all():
                    event.delivered_at = datetime.now(UTC)
        job.locked_until = None


async def run_once(provider: MockPaySwiftClient | None = None) -> bool:
    job = await claim_job()
    if job is None:
        return False
    provider = provider or MockPaySwiftClient(get_settings())
    try:
        if job.kind == "MESSAGE":
            await process_message(job.payload["message_id"], provider, get_settings())
            result = "DONE"
        elif job.kind == "PAYMENT":
            status = await execute_payment(job.payload["authorization_id"], provider)
            result = "RETRY" if status == "RECONCILING" else "DONE"
        else:
            raise ValueError(f"unknown job type {job.kind}")
    except Exception:
        log.exception("Job %s failed", job.id)
        result = "RETRY"
    await finish_job(job.id, result)
    return True


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    provider = MockPaySwiftClient(get_settings())
    heartbeat = Path(tempfile.gettempdir()) / "quickdrop-worker-heartbeat"
    last_sweep = 0.0
    while True:
        heartbeat.touch()
        current = asyncio.get_running_loop().time()
        if current - last_sweep > 30:
            try:
                await reconcile_existing(provider)
            except Exception:
                log.exception("Reconciliation sweep failed")
            last_sweep = current
        if not await run_once(provider):
            await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
