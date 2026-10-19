"""HTTP boundary for the fictional MockPaySwift payment provider."""

from __future__ import annotations

import hmac
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from .config import ProviderSettings
from .models import (
    FaultCreate,
    OutageCreate,
    PaymentCreate,
    PaymentView,
    ResolveCreate,
    SeedBatch,
)
from .service import ProviderError, SQLiteLedger


def create_app(settings: ProviderSettings | None = None) -> FastAPI:
    settings = settings or ProviderSettings.from_env()
    ledger = SQLiteLedger(settings.database_path)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await run_in_threadpool(ledger.initialize)
        yield

    app = FastAPI(
        title="MockPaySwift (simulated payments)",
        description="Fictional provider simulator. No real funds or bank accounts are used.",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.ledger = ledger

    @app.exception_handler(ProviderError)
    async def provider_error_handler(_, exc: ProviderError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    def require_auth(authorization: str | None = Header(default=None)) -> None:
        if not settings.api_key:
            raise HTTPException(status_code=503, detail="provider API key is not configured")
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="provider authentication required")
        supplied = authorization.removeprefix("Bearer ")
        if not hmac.compare_digest(supplied, settings.api_key):
            raise HTTPException(status_code=401, detail="invalid provider credentials")

    def require_test(
        x_test_token: str | None = Header(default=None),
        _: None = Depends(require_auth),
    ) -> None:
        if not settings.test_mode:
            raise HTTPException(status_code=404, detail="not found")
        if not settings.test_token:
            raise HTTPException(status_code=503, detail="test token is not configured")
        if not x_test_token or not hmac.compare_digest(x_test_token, settings.test_token):
            raise HTTPException(status_code=403, detail="test authorization required")

    def require_available(_: None = Depends(require_auth)) -> None:
        if ledger.is_outage():
            raise HTTPException(status_code=503, detail="simulated provider unavailable")

    @app.get("/health")
    def health() -> dict[str, str | bool]:
        try:
            healthy = ledger.check() and not ledger.is_outage()
        except Exception as exc:
            raise HTTPException(status_code=503, detail="provider ledger unavailable") from exc
        if not healthy:
            raise HTTPException(status_code=503, detail="provider ledger unavailable")
        return {"status": "ok", "service": "MockPaySwift", "simulated": True}

    @app.post("/v1/payments", response_model=PaymentView, status_code=201)
    def submit_payment(
        request: PaymentCreate, response: Response, _: None = Depends(require_available)
    ) -> dict:
        payment, mode, replayed = ledger.submit(request)
        if replayed:
            response.status_code = 200
        if mode == "timeout_after_create":
            raise HTTPException(status_code=504, detail="simulated timeout after payment creation")
        if mode == "unexpected_response":
            # Deliberately violates the normal provider shape to exercise client recovery.
            return JSONResponse(status_code=200, content={"unexpected": "simulated_response"})
        return payment

    @app.get("/v1/payments/by-idempotency-key/{key}", response_model=PaymentView)
    def payment_by_key(key: str, _: None = Depends(require_available)) -> dict:
        return ledger.get_by_key(key)

    @app.get("/v1/payments/{payment_id}/events")
    def payment_events(payment_id: str, _: None = Depends(require_available)) -> dict:
        return {"items": ledger.history(payment_id), "simulated": True}

    @app.get("/v1/payments/{payment_id}", response_model=PaymentView)
    def payment_by_id(payment_id: str, _: None = Depends(require_available)) -> dict:
        return ledger.get_by_id(payment_id)

    @app.get("/v1/riders/{rider_id}/transactions")
    def rider_transactions(
        rider_id: str,
        limit: int = Query(default=100, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
        _: None = Depends(require_available),
    ) -> dict:
        return ledger.list_rider(rider_id, limit=limit, offset=offset)

    @app.post("/test/faults", status_code=204)
    def configure_fault(fault: FaultCreate, _: None = Depends(require_test)) -> Response:
        ledger.configure_fault(fault)
        return Response(status_code=204)

    @app.delete("/test/faults/{key}", status_code=204)
    def clear_fault(key: str, _: None = Depends(require_test)) -> Response:
        ledger.clear_fault(key)
        return Response(status_code=204)

    @app.post("/test/payments/{payment_id}/resolve", response_model=PaymentView)
    def resolve_payment(
        payment_id: str, request: ResolveCreate, _: None = Depends(require_test)
    ) -> dict:
        return ledger.resolve(payment_id, request.status)

    @app.post("/test/payments/{payment_id}/reverse", response_model=PaymentView)
    def reverse_payment(payment_id: str, _: None = Depends(require_test)) -> dict:
        return ledger.reverse(payment_id)

    @app.post("/test/seed-payments")
    def seed_payments(batch: SeedBatch, _: None = Depends(require_test)) -> dict:
        return {**ledger.seed_batch(batch.payments), "simulated": True}

    @app.post("/test/outage", status_code=204)
    def set_outage(request: OutageCreate, _: None = Depends(require_test)) -> Response:
        ledger.set_outage(request.unavailable)
        return Response(status_code=204)

    @app.post("/test/reset", status_code=204)
    def reset(_: None = Depends(require_test)) -> Response:
        ledger.reset()
        return Response(status_code=204)

    return app


app = create_app()
