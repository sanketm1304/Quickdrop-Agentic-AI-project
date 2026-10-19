"""QuickDrop HTTP API and shared rider/employee browser experience."""

import asyncio
import hmac
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select, text

from app.config import get_settings
from app.database import SessionLocal, engine
from app.integrations.mockpayswift_client import (
    MockPaySwiftClient,
    ProviderProtocolError,
    ProviderUnavailable,
)
from app.models import (
    Approval,
    AuditEvent,
    Conversation,
    Dispute,
    DisputeEvidence,
    DisputeRevision,
    EarningAdjustmentAllocation,
    Employee,
    Job,
    Message,
    OutboxEvent,
    PaymentAttempt,
    PaymentAuthorization,
    PaymentReconciliation,
    Rider,
)
from app.policy import POLICY_VERSION
from app.security import (
    RateLimiter,
    new_login_challenge,
    new_session,
    read_session,
    require_bearer,
    require_csrf,
    require_identity,
    require_login_csrf,
    verify_password,
)
from app.services.messages import enqueue_message
from app.services.payments import reserve_correction

settings = get_settings()
provider = MockPaySwiftClient(settings)
app = FastAPI(title="QuickDrop synthetic payout support", version="0.1.0")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
login_limiter = RateLimiter(8, 60)
message_limiter = RateLimiter(30, 60)
REVIEW_STATES = {
    "PENDING_APPROVAL",
    "UNDER_REVIEW",
    "RECONCILIATION_REVIEW",
    "PAYMENT_FAILED",
    "NEEDS_CLARIFICATION",
}
RESOLVED_STATES = {"PAID", "NO_BALANCE", "REJECTED"}
OPS_PAGE_SIZE = 100


@app.middleware("http")
async def protect_request(request: Request, call_next):
    if request.method in {"POST", "PUT", "PATCH"}:
        length = request.headers.get("content-length")
        try:
            declared_length = int(length) if length is not None else None
        except ValueError:
            return JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)
        if declared_length is not None and declared_length > 16_384:
            return JSONResponse({"detail": "Request too large"}, status_code=413)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 16_384:
                return JSONResponse({"detail": "Request too large"}, status_code=413)
        request._body = bytes(body)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' https://cdn.jsdelivr.net; "
        "style-src 'self' https://cdn.jsdelivr.net; font-src 'self' data:; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
    )
    return response


class MessagePayload(BaseModel):
    message_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._:-]+$")
    rider_id: str = Field(min_length=2, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    text: str = Field(min_length=1, max_length=2000)
    received_at: datetime


@app.get("/health")
async def health():
    return {"status": "ok", "service": "QuickDrop", "simulated_payments": True}


@app.get("/ready")
async def ready():
    try:
        async with SessionLocal() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(503, "Database unavailable") from exc
    return {"status": "ready"}


@app.post("/messages")
async def webhook(payload: MessagePayload, request: Request):
    require_bearer(request, settings.webhook_token)
    message_limiter.check(f"webhook:{payload.rider_id}")
    try:
        reply = await enqueue_message(**payload.model_dump())
    except LookupError as exc:
        raise HTTPException(404, "Rider not found") from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"reply": reply}


def _browser_auth(request: Request, role: str) -> dict:
    try:
        return require_identity(request, settings, role)
    except HTTPException:
        raise HTTPException(401, "Sign in required")


async def _ops_identity(request: Request) -> tuple[dict, Employee]:
    identity = _browser_auth(request, "reviewer")
    async with SessionLocal() as session:
        employee = await session.get(Employee, identity["id"])
    if not employee or not employee.active or employee.role not in {"reviewer", "admin"}:
        raise HTTPException(403, "Employee access revoked")
    return identity, employee


def _set_session(response, identity: str, role: str):
    signed, _ = new_session(settings, identity, role)
    response.set_cookie(
        "qd_session",
        signed,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        max_age=8 * 3600,
    )


def _login_response(request: Request, template: str, error: str | None = None, status: int = 200):
    signed, csrf = new_login_challenge(settings)
    response = templates.TemplateResponse(
        request, template, {"error": error, "csrf_token": csrf}, status_code=status
    )
    response.set_cookie(
        "qd_login",
        signed,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        max_age=30 * 60,
    )
    return response


@app.get("/", include_in_schema=False)
async def home(request: Request):
    session = read_session(request, settings)
    return RedirectResponse(
        "/ops"
        if session and session["role"] in {"reviewer", "admin"}
        else "/rider"
        if session
        else "/login",
        status_code=303,
    )


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def rider_login_page(request: Request):
    return _login_response(request, "rider_login.html")


@app.post("/login", include_in_schema=False)
async def rider_login(
    request: Request,
    rider_id: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
):
    require_login_csrf(request, settings, csrf_token)
    login_limiter.check(f"rider:{request.client.host if request.client else 'unknown'}")
    async with SessionLocal() as session:
        rider = await session.get(Rider, rider_id)
    if not rider or rider.status != "ACTIVE" or not verify_password(password, rider.password_hash):
        return _login_response(request, "rider_login.html", "Invalid account or password", 401)
    response = RedirectResponse("/rider", status_code=303)
    _set_session(response, rider_id, "rider")
    response.delete_cookie("qd_login")
    return response


@app.post("/logout", include_in_schema=False)
async def rider_logout(request: Request, csrf_token: str = Form(...)):
    identity = _browser_auth(request, "rider")
    require_csrf(request, identity, csrf_token)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("qd_session")
    return response


async def _thread(rider_id: str) -> list[dict]:
    async with SessionLocal() as session:
        rows = (
            await session.scalars(
                select(Message)
                .where(Message.rider_id == rider_id)
                .order_by(Message.sequence)
                .limit(200)
            )
        ).all()
    items = []
    for row in rows:
        items.append(
            {"sender": "rider", "text": row.text, "created_at": row.received_at.isoformat()}
        )
        if row.reply:
            items.append(
                {"sender": "assistant", "text": row.reply, "created_at": row.created_at.isoformat()}
            )
        elif row.status == "QUEUED":
            items.append(
                {
                    "sender": "assistant",
                    "text": "Checking your records…",
                    "created_at": row.created_at.isoformat(),
                }
            )
    return items


async def _case_rows(
    rider_id: str | None = None,
    q: str = "",
    status: str = "",
    *,
    case_id: str | None = None,
    limit: int = OPS_PAGE_SIZE,
    offset: int = 0,
) -> list[dict]:
    async with SessionLocal() as session:
        query = select(Dispute)
        if rider_id:
            query = query.where(Dispute.rider_id == rider_id)
        if case_id:
            query = query.where(Dispute.id == case_id)
        if status == "ESCALATED":
            query = query.where(Dispute.status.in_(REVIEW_STATES))
        elif status == "RESOLVED":
            query = query.where(Dispute.status.in_(RESOLVED_STATES))
        elif status:
            query = query.where(Dispute.status == status)
        if q:
            pattern = f"%{q[:100]}%"
            query = query.where(
                or_(
                    Dispute.id.ilike(pattern),
                    Dispute.rider_id.ilike(pattern),
                    Dispute.trip_id.ilike(pattern),
                )
            )
        disputes = (
            await session.scalars(
                query.order_by(Dispute.updated_at.desc(), Dispute.id.desc())
                .offset(offset)
                .limit(limit)
            )
        ).all()
        auths = (
            (
                await session.scalars(
                    select(PaymentAuthorization)
                    .where(PaymentAuthorization.dispute_id.in_([row.id for row in disputes]))
                    .order_by(PaymentAuthorization.created_at.desc())
                )
            ).all()
            if disputes
            else []
        )
    latest_auth = {}
    for auth in auths:
        latest_auth.setdefault(auth.dispute_id, auth)
    gate = asyncio.Semaphore(10)

    async def observe(auth):
        if auth is None:
            return "NOT_AUTHORIZED", None
        async with gate:
            try:
                payment = await provider.by_key(auth.idempotency_key)
                return (payment.status, payment.payment_id) if payment else ("UNKNOWN", None)
            except (ProviderUnavailable, ProviderProtocolError):
                return "UNKNOWN", None

    observations = await asyncio.gather(*(observe(latest_auth.get(row.id)) for row in disputes))
    return [
        {
            "id": dispute.id,
            "rider_id": dispute.rider_id,
            "category": dispute.category,
            "trip_id": dispute.trip_id,
            "status": dispute.status,
            "claimed_amount": dispute.claimed_amount,
            "verified_outstanding": dispute.verified_outstanding,
            "reserved_amount": dispute.reserved_amount,
            "available_to_authorize": dispute.available_to_authorize,
            "payment_status": payment_status,
            "payment_reference": payment_reference,
            "updated_at": dispute.updated_at.isoformat(),
            "revision": dispute.revision,
            "policy_version": POLICY_VERSION,
            "policy_reason": dispute.explanation,
        }
        for dispute, (payment_status, payment_reference) in zip(disputes, observations, strict=True)
    ]


@app.get("/rider", response_class=HTMLResponse, include_in_schema=False)
async def rider_page(request: Request):
    identity = _browser_auth(request, "rider")
    async with SessionLocal() as session:
        rider = await session.get(Rider, identity["id"])
    return templates.TemplateResponse(
        request,
        "rider.html",
        {
            "rider": {"rider_id": rider.id, "display_name": rider.display_name},
            "messages": await _thread(rider.id),
            "disputes": await _case_rows(rider.id),
            "csrf_token": identity["csrf"],
        },
    )


@app.get("/rider/thread", response_class=HTMLResponse, include_in_schema=False)
async def rider_thread(request: Request):
    identity = _browser_auth(request, "rider")
    return templates.TemplateResponse(
        request, "_rider_thread.html", {"messages": await _thread(identity["id"])}
    )


@app.get("/rider/status", response_class=HTMLResponse, include_in_schema=False)
async def rider_status(request: Request):
    identity = _browser_auth(request, "rider")
    return templates.TemplateResponse(
        request, "_rider_cases.html", {"disputes": await _case_rows(identity["id"])}
    )


@app.post("/rider/messages", include_in_schema=False)
async def rider_message(request: Request, text: str = Form(...), csrf_token: str = Form(...)):
    identity = _browser_auth(request, "rider")
    require_csrf(request, identity, csrf_token)
    message_limiter.check(f"rider:{identity['id']}")
    await enqueue_message(
        message_id=f"web.{uuid4().hex}",
        rider_id=identity["id"],
        text=text,
        received_at=datetime.now(UTC),
    )
    if request.headers.get("HX-Request", "").lower() == "true":
        return templates.TemplateResponse(
            request, "_rider_thread.html", {"messages": await _thread(identity["id"])}
        )
    return RedirectResponse("/rider", status_code=303)


@app.get("/rider/disputes")
async def rider_disputes(request: Request):
    identity = _browser_auth(request, "rider")
    return {"items": await _case_rows(identity["id"])}


@app.get("/ops/login", response_class=HTMLResponse, include_in_schema=False)
async def ops_login_page(request: Request):
    return _login_response(request, "ops_login.html")


@app.post("/ops/login", include_in_schema=False)
async def ops_login(
    request: Request,
    employee_id: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
):
    require_login_csrf(request, settings, csrf_token)
    login_limiter.check(f"ops:{request.client.host if request.client else 'unknown'}")
    async with SessionLocal() as session:
        employee = await session.get(Employee, employee_id)
    if not employee or not employee.active or not verify_password(password, employee.password_hash):
        return _login_response(request, "ops_login.html", "Invalid account or password", 401)
    response = RedirectResponse("/ops", status_code=303)
    _set_session(response, employee_id, employee.role)
    response.delete_cookie("qd_login")
    return response


@app.post("/ops/logout", include_in_schema=False)
async def ops_logout(request: Request, csrf_token: str = Form(...)):
    identity, _ = await _ops_identity(request)
    require_csrf(request, identity, csrf_token)
    response = RedirectResponse("/ops/login", status_code=303)
    response.delete_cookie("qd_session")
    return response


async def _ops_metrics() -> dict:
    async with SessionLocal() as session:
        rows = (await session.scalars(select(Dispute))).all()
        reviewed_ids = set((await session.scalars(select(Approval.dispute_id))).all())
        confirmed = await session.scalar(
            select(func.count())
            .select_from(PaymentAuthorization)
            .where(PaymentAuthorization.status == "CONFIRMED")
        )
    resolved = [row for row in rows if row.status in RESOLVED_STATES]
    average = (
        sum((row.updated_at - row.created_at).total_seconds() for row in resolved) / len(resolved)
        if resolved
        else None
    )
    return {
        "total_disputes": len(rows),
        "open_disputes": sum(row.status not in RESOLVED_STATES for row in rows),
        "auto_resolved": sum(
            row.status in {"PAID", "NO_BALANCE"} and row.id not in reviewed_ids for row in rows
        ),
        "pending_approvals": sum(row.status == "PENDING_APPROVAL" for row in rows),
        "escalated_cases": sum(row.status in REVIEW_STATES for row in rows),
        "confirmed_adjustments": confirmed or 0,
        "failed_or_uncertain": sum(
            row.status in {"PAYMENT_FAILED", "RECONCILING", "RECONCILIATION_REVIEW"} for row in rows
        ),
        "average_resolution_time": f"{average:.0f}s" if average is not None else "—",
    }


@app.get("/ops", response_class=HTMLResponse, include_in_schema=False)
async def ops_page(request: Request, q: str = "", status: str = "", page: int = Query(1, ge=1)):
    identity, employee = await _ops_identity(request)
    rows = await _case_rows(
        q=q,
        status=status,
        limit=OPS_PAGE_SIZE + 1,
        offset=(page - 1) * OPS_PAGE_SIZE,
    )
    return templates.TemplateResponse(
        request,
        "ops.html",
        {
            "employee": {
                "employee_id": employee.id,
                "display_name": employee.display_name,
                "role": employee.role,
            },
            "metrics": await _ops_metrics(),
            "cases": rows[:OPS_PAGE_SIZE],
            "q": q,
            "status": status,
            "page": page,
            "has_next": len(rows) > OPS_PAGE_SIZE,
            "csrf_token": identity["csrf"],
        },
    )


@app.get("/ops/pending")
async def ops_pending(request: Request, page: int = Query(1, ge=1)):
    await _ops_identity(request)
    rows = await _case_rows(
        status="ESCALATED",
        limit=OPS_PAGE_SIZE + 1,
        offset=(page - 1) * OPS_PAGE_SIZE,
    )
    return {
        "items": rows[:OPS_PAGE_SIZE],
        "page": page,
        "next_page": page + 1 if len(rows) > OPS_PAGE_SIZE else None,
    }


async def _case_detail(case_id: str) -> dict:
    async with SessionLocal() as session:
        dispute = await session.get(Dispute, case_id)
        if dispute is None:
            raise HTTPException(404, "Case not found")
        evidence = (
            await session.scalars(
                select(DisputeEvidence)
                .where(DisputeEvidence.dispute_id == case_id)
                .order_by(DisputeEvidence.created_at)
            )
        ).all()
        audit = (
            await session.scalars(
                select(AuditEvent)
                .where(AuditEvent.dispute_id == case_id)
                .order_by(AuditEvent.created_at)
            )
        ).all()
        messages = (
            await session.scalars(
                select(Message)
                .where(Message.conversation_id == dispute.conversation_id)
                .order_by(Message.sequence)
            )
        ).all()
    summary = (await _case_rows(rider_id=dispute.rider_id, case_id=case_id, limit=1))[0]
    summary["evidence"] = [
        {"kind": row.kind, "reference": row.reference, "detail": str(row.details)}
        for row in evidence
    ]
    summary["audit_events"] = [
        {
            "event_type": row.event_type,
            "actor": row.actor,
            "detail": str(row.details),
            "created_at": row.created_at.isoformat(),
        }
        for row in audit
    ]
    summary["messages"] = []
    for row in messages:
        summary["messages"].append(
            {"sender": "rider", "text": row.text, "created_at": row.received_at.isoformat()}
        )
        if row.reply:
            summary["messages"].append(
                {"sender": "assistant", "text": row.reply, "created_at": row.created_at.isoformat()}
            )
    return summary


@app.get("/ops/cases/{case_id}", response_class=HTMLResponse, include_in_schema=False)
async def ops_case(request: Request, case_id: str, notice: str = "", error: str = ""):
    identity, employee = await _ops_identity(request)
    case = await _case_detail(case_id)
    return templates.TemplateResponse(
        request,
        "ops_case.html",
        {
            "employee": {
                "employee_id": employee.id,
                "display_name": employee.display_name,
                "role": employee.role,
            },
            "case": case,
            "csrf_token": identity["csrf"],
            "can_decide": employee.role in {"reviewer", "admin"}
            and case["status"] in {"PENDING_APPROVAL", "UNDER_REVIEW", "NEEDS_CLARIFICATION"},
            "notice": notice,
            "error": error,
        },
    )


@app.post("/ops/cases/{case_id}/approve", include_in_schema=False)
async def approve_case(
    request: Request,
    case_id: str,
    reason: str = Form(...),
    revision: int = Form(...),
    csrf_token: str = Form(...),
):
    identity, _ = await _ops_identity(request)
    require_csrf(request, identity, csrf_token)
    if not 10 <= len(reason.strip()) <= 1000:
        raise HTTPException(422, "A review reason of 10–1000 characters is required")
    outcome, _amount = await reserve_correction(
        case_id,
        provider,
        mode="HUMAN",
        expected_revision=revision,
        reviewer_id=identity["id"],
        review_reason=reason.strip(),
    )
    if outcome != "RESERVED":
        raise HTTPException(409, f"Approval rejected after revalidation: {outcome}")
    return RedirectResponse(
        f"/ops/cases/{case_id}?notice=Simulated+correction+queued", status_code=303
    )


@app.post("/ops/cases/{case_id}/reject", include_in_schema=False)
async def reject_case(
    request: Request,
    case_id: str,
    reason: str = Form(...),
    revision: int = Form(...),
    csrf_token: str = Form(...),
):
    identity, _ = await _ops_identity(request)
    require_csrf(request, identity, csrf_token)
    if not 10 <= len(reason.strip()) <= 1000:
        raise HTTPException(422, "A review reason of 10–1000 characters is required")
    async with SessionLocal.begin() as session:
        dispute = await session.scalar(
            select(Dispute).where(Dispute.id == case_id).with_for_update()
        )
        if dispute is None:
            raise HTTPException(404, "Case not found")
        if dispute.revision != revision or dispute.status not in {
            "PENDING_APPROVAL",
            "UNDER_REVIEW",
            "NEEDS_CLARIFICATION",
        }:
            raise HTTPException(409, "The case changed; reload before deciding")
        dispute.status = "REJECTED"
        dispute.explanation = reason.strip()
        session.add(
            Approval(
                dispute_id=case_id,
                employee_id=identity["id"],
                revision=revision,
                decision="REJECTED",
                reason=reason.strip(),
            )
        )
        session.add(
            AuditEvent(
                rider_id=dispute.rider_id,
                dispute_id=case_id,
                event_type="HUMAN_REJECTED",
                actor=f"employee:{identity['id']}",
                details={"revision": revision, "reason": reason.strip()},
            )
        )
    return RedirectResponse(f"/ops/cases/{case_id}?notice=Case+rejected", status_code=303)


@app.get("/trace/{rider_id}")
async def trace_rider(request: Request, rider_id: str):
    identity = read_session(request, settings)
    if not identity or (identity["role"] == "rider" and identity["id"] != rider_id):
        raise HTTPException(403, "Not authorized for this trace")
    if identity["role"] != "rider":
        await _ops_identity(request)
    async with SessionLocal() as session:
        events = (
            await session.scalars(
                select(AuditEvent)
                .where(AuditEvent.rider_id == rider_id)
                .order_by(AuditEvent.created_at, AuditEvent.id)
                .limit(1000)
            )
        ).all()
    return {
        "rider_id": rider_id,
        "events": [
            {
                "event_type": row.event_type,
                "actor": row.actor,
                "dispute_id": row.dispute_id,
                "details": row.details,
                "created_at": row.created_at.isoformat(),
            }
            for row in events
        ],
    }


@app.post("/test/reset-fixtures")
async def reset_fixtures(request: Request):
    if not settings.quickdrop_test_mode:
        raise HTTPException(404, "Not found")
    require_bearer(request, settings.webhook_token)
    supplied = request.headers.get("X-Test-Token", "")
    if not supplied or not hmac.compare_digest(supplied, settings.mockpayswift_test_token):
        raise HTTPException(403, "Test authorization required")
    try:
        async with httpx.AsyncClient(base_url=settings.mockpayswift_url, timeout=15) as client:
            response = await client.post(
                "/test/reset",
                headers={
                    "Authorization": f"Bearer {settings.mockpayswift_api_key}",
                    "X-Test-Token": settings.mockpayswift_test_token,
                },
            )
            response.raise_for_status()
        from scripts.seed_database import seed_provider_from_db

        await seed_provider_from_db(settings, engine=engine, update_cache=False)
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(503, "Fixture provider reset failed") from exc
    async with SessionLocal.begin() as session:
        if session.bind.dialect.name == "postgresql":
            await session.execute(text("SELECT public.quickdrop_test_reset_fixtures()"))
        else:
            for model in (
                PaymentReconciliation,
                PaymentAttempt,
                EarningAdjustmentAllocation,
                Approval,
                DisputeEvidence,
                DisputeRevision,
                PaymentAuthorization,
                AuditEvent,
                Job,
                OutboxEvent,
                Dispute,
                Message,
                Conversation,
            ):
                await session.execute(delete(model))
    return Response(status_code=204)
