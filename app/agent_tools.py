"""Read-only, rider-scoped callbacks exposed to the single agent."""

from sqlalchemy import select

from app.agent.tool_executor import ToolExecutor
from app.database import SessionLocal
from app.integrations.mockpayswift_client import MockPaySwiftClient
from app.models import Dispute, EarningComponent, EarningPeriod, PayoutRecord, Trip
from app.policy import AUTO_LIMIT, POLICY_VERSION
from app.services.financial import select_component_allocations, verify_period


def make_tool_executor(provider: MockPaySwiftClient) -> ToolExecutor:
    async def get_rider_trips(rider_id, args):
        async with SessionLocal() as session:
            query = select(Trip).where(Trip.rider_id == rider_id)
            if args.earning_date:
                query = query.where(Trip.trip_date == args.earning_date)
            rows = (
                await session.scalars(
                    query.order_by(Trip.trip_date.desc(), Trip.id).limit(args.limit)
                )
            ).all()
            return {
                "items": [
                    {
                        "trip_id": row.id,
                        "date": row.trip_date.isoformat(),
                        "status": row.status,
                        "distance_km": str(row.distance_km),
                        "surge_category": row.surge_category,
                    }
                    for row in rows
                ]
            }

    async def get_trip_details(rider_id, args):
        async with SessionLocal() as session:
            row = await session.scalar(
                select(Trip).where(Trip.id == args.trip_id, Trip.rider_id == rider_id)
            )
            if row is None:
                return {"found": False}
            return {
                "found": True,
                "trip_id": row.id,
                "date": row.trip_date.isoformat(),
                "status": row.status,
                "distance_km": str(row.distance_km),
                "surge_category": row.surge_category,
                "policy_version": row.policy_version,
            }

    async def get_payout_history(rider_id, args):
        async with SessionLocal() as session:
            query = (
                select(PayoutRecord).join(EarningPeriod).where(PayoutRecord.rider_id == rider_id)
            )
            if args.earning_date:
                query = query.where(EarningPeriod.business_date == args.earning_date)
            rows = (
                await session.scalars(
                    query.order_by(PayoutRecord.created_at.desc()).limit(args.limit)
                )
            ).all()
            return {
                "items": [
                    {
                        "payout_id": row.id,
                        "period_id": row.period_id,
                        "amount": str(row.amount),
                        "provider_key": row.idempotency_key,
                    }
                    for row in rows
                ]
            }

    async def get_provider_transactions(rider_id, args):
        rows = await provider.rider_transactions(rider_id, args.limit)
        return {
            "items": [
                {
                    "reference": row.payment_id,
                    "dispute_reference": row.dispute_reference,
                    "amount": str(row.amount),
                    "status": row.status,
                }
                for row in rows
            ]
        }

    async def get_applicable_policy(rider_id, args):
        return {
            "policy_version": POLICY_VERSION,
            "base_pay": "40.00",
            "distance_per_km": "8.00",
            "surges": {"NONE": "0.00", "STANDARD": "15.00", "HIGH": "30.00"},
            "incentive_per_10_trips": "50.00",
            "auto_limit": str(AUTO_LIMIT),
            "timezone": "Asia/Kolkata",
        }

    async def calculate_verified_outstanding(rider_id, args):
        async with SessionLocal() as session:
            period = None
            if args.trip_id:
                trip = await session.scalar(
                    select(Trip).where(Trip.id == args.trip_id, Trip.rider_id == rider_id)
                )
                if trip:
                    period = await session.scalar(
                        select(EarningPeriod).where(
                            EarningPeriod.rider_id == rider_id,
                            EarningPeriod.business_date == trip.trip_date,
                        )
                    )
            elif args.earning_date:
                period = await session.scalar(
                    select(EarningPeriod).where(
                        EarningPeriod.rider_id == rider_id,
                        EarningPeriod.business_date == args.earning_date,
                    )
                )
            if not period:
                return {"found": False, "reason": "authorized earning period not found"}
            verification = await verify_period(session, period.id, provider)
            components = (
                await session.scalars(
                    select(EarningComponent).where(EarningComponent.period_id == period.id)
                )
            ).all()
            category = {
                "surge": "SURGE",
                "incentive": "INCENTIVE",
                "base_pay": "BASE",
                "partial_payout": "TOTAL",
                "total_payout": "TOTAL",
            }.get(args.category.value, "UNSUPPORTED")
            allocations = select_component_allocations(
                components, verification, category, args.trip_id
            )
            return {
                "found": True,
                "period_id": period.id,
                "expected": str(verification.expected),
                "confirmed_paid": str(verification.confirmed_paid),
                "reserved": str(verification.reserved),
                "available": str(verification.available),
                "issue_outstanding": str(sum(allocations.values())),
                "uncertain": verification.uncertain,
                "reason": verification.reason,
            }

    async def get_dispute_history(rider_id, args):
        async with SessionLocal() as session:
            rows = (
                await session.scalars(
                    select(Dispute)
                    .where(Dispute.rider_id == rider_id)
                    .order_by(Dispute.created_at.desc())
                    .limit(args.limit)
                )
            ).all()
            return {
                "items": [
                    {
                        "id": row.id,
                        "category": row.category,
                        "trip_id": row.trip_id,
                        "status": row.status,
                        "claimed_amount": str(row.claimed_amount)
                        if row.claimed_amount is not None
                        else None,
                    }
                    for row in rows
                ]
            }

    return ToolExecutor(
        {
            "get_rider_trips": get_rider_trips,
            "get_trip_details": get_trip_details,
            "get_payout_history": get_payout_history,
            "get_provider_transactions": get_provider_transactions,
            "get_applicable_policy": get_applicable_policy,
            "calculate_verified_outstanding": calculate_verified_outstanding,
            "get_dispute_history": get_dispute_history,
        }
    )
