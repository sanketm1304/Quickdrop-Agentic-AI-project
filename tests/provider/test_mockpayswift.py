"""Provider contract tests; no real payment network is contacted."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from mockpayswift.config import ProviderSettings
from mockpayswift.main import create_app
from mockpayswift.models import PaymentCreate
from mockpayswift.service import ProviderError, SQLiteLedger

API_HEADERS = {"Authorization": "Bearer provider-secret"}
TEST_HEADERS = {**API_HEADERS, "X-Test-Token": "test-secret"}


def payload(key: str = "correction-1", **changes: str) -> dict[str, str]:
    data = {
        "idempotency_key": key,
        "rider_id": "R-001",
        "dispute_reference": "D-001",
        "amount": "120.00",
        "currency": "INR",
        "source_ref": "trip:T-001:surge",
        "earning_period": "2026-10-17",
    }
    return {**data, **changes}


@pytest.fixture
def provider(tmp_path):
    settings = ProviderSettings(
        database_path=str(tmp_path / "provider.sqlite3"),
        api_key="provider-secret",
        test_mode=True,
        test_token="test-secret",
    )
    with TestClient(create_app(settings)) as client:
        yield client


def test_requires_authentication_and_marks_simulation(provider: TestClient):
    assert provider.get("/health").json()["simulated"] is True
    assert provider.post("/v1/payments", json=payload()).status_code == 401
    assert (
        provider.post(
            "/v1/payments", json=payload(), headers={"Authorization": "Bearer wrong"}
        ).status_code
        == 401
    )
    response = provider.post("/v1/payments", json=payload(), headers=API_HEADERS)
    assert response.status_code == 201
    assert response.json()["simulated"] is True
    assert response.json()["amount"] == "120.00"
    assert response.json()["status"] == "CONFIRMED"
    assert response.json()["payment_id"].startswith("mps_")
    assert response.json()["settled_at"] is not None


def test_idempotency_conflict_history_and_rider_lookup(provider: TestClient):
    first = provider.post("/v1/payments", json=payload(), headers=API_HEADERS).json()
    replay = provider.post("/v1/payments", json=payload(amount="120"), headers=API_HEADERS)
    assert replay.status_code == 200
    assert replay.json()["payment_id"] == first["payment_id"]
    conflict = provider.post("/v1/payments", json=payload(amount="121.00"), headers=API_HEADERS)
    assert conflict.status_code == 409
    assert (
        provider.get(f"/v1/payments/{first['payment_id']}", headers=API_HEADERS).json()[
            "payment_id"
        ]
        == first["payment_id"]
    )
    assert (
        provider.get("/v1/payments/by-idempotency-key/correction-1", headers=API_HEADERS).json()[
            "payment_id"
        ]
        == first["payment_id"]
    )
    rows = provider.get("/v1/riders/R-001/transactions", headers=API_HEADERS).json()
    assert [item["payment_id"] for item in rows["items"]] == [first["payment_id"]]
    assert rows["next_offset"] is None
    events = provider.get(f"/v1/payments/{first['payment_id']}/events", headers=API_HEADERS).json()[
        "items"
    ]
    assert [event["to_status"] for event in events] == ["CREATED", "PENDING", "CONFIRMED"]


def test_timeout_after_create_is_resolved_by_idempotency_lookup(provider: TestClient):
    assert (
        provider.post(
            "/test/faults",
            json={"idempotency_key": "timeout-1", "mode": "timeout_after_create"},
            headers=TEST_HEADERS,
        ).status_code
        == 204
    )
    submission = provider.post("/v1/payments", json=payload("timeout-1"), headers=API_HEADERS)
    assert submission.status_code == 504
    lookup = provider.get("/v1/payments/by-idempotency-key/timeout-1", headers=API_HEADERS)
    assert lookup.status_code == 200
    assert lookup.json()["status"] == "CONFIRMED"
    replay = provider.post("/v1/payments", json=payload("timeout-1"), headers=API_HEADERS)
    assert replay.status_code == 200
    assert replay.json()["payment_id"] == lookup.json()["payment_id"]


def test_one_shot_prefix_fault_records_timeout_event(provider: TestClient):
    configured = provider.post(
        "/test/faults",
        json={"idempotency_prefix": "QD-AUTH-", "mode": "timeout_after_create"},
        headers=TEST_HEADERS,
    )
    assert configured.status_code == 204

    first = provider.post("/v1/payments", json=payload("QD-AUTH-first"), headers=API_HEADERS)
    assert first.status_code == 504
    payment = provider.get(
        "/v1/payments/by-idempotency-key/QD-AUTH-first", headers=API_HEADERS
    ).json()
    assert payment["status"] == "CONFIRMED"
    events = provider.get(
        f"/v1/payments/{payment['payment_id']}/events", headers=API_HEADERS
    ).json()["items"]
    assert any(event["reason"] == "simulated_timeout_after_create" for event in events)

    second = provider.post("/v1/payments", json=payload("QD-AUTH-second"), headers=API_HEADERS)
    assert second.status_code == 201
    assert second.json()["status"] == "CONFIRMED"

    assert (
        provider.post(
            "/test/faults",
            json={"idempotency_prefix": "QD-AUTH-", "mode": "timeout_after_create"},
            headers=TEST_HEADERS,
        ).status_code
        == 204
    )
    assert provider.delete("/test/faults/prefix:QD-AUTH-", headers=TEST_HEADERS).status_code == 204
    assert (
        provider.post(
            "/v1/payments", json=payload("QD-AUTH-third"), headers=API_HEADERS
        ).status_code
        == 201
    )


def test_delayed_and_failed_settlement(provider: TestClient):
    provider.post(
        "/test/faults",
        json={"idempotency_key": "delay-1", "mode": "delayed", "delay_ms": 80},
        headers=TEST_HEADERS,
    )
    pending = provider.post("/v1/payments", json=payload("delay-1"), headers=API_HEADERS)
    assert pending.status_code == 201
    assert pending.json()["status"] == "PENDING"
    assert pending.json()["settled_at"] is None
    time.sleep(0.12)
    confirmed = provider.get(f"/v1/payments/{pending.json()['payment_id']}", headers=API_HEADERS)
    assert confirmed.json()["status"] == "CONFIRMED"
    provider.post(
        "/test/faults",
        json={"idempotency_key": "failed-1", "mode": "failed_settlement"},
        headers=TEST_HEADERS,
    )
    failed = provider.post("/v1/payments", json=payload("failed-1"), headers=API_HEADERS)
    assert failed.json()["status"] == "FAILED"
    assert failed.json()["settled_at"] is None


def test_reversal_and_uncertain_resolution_are_test_authorized(provider: TestClient):
    confirmed = provider.post("/v1/payments", json=payload(), headers=API_HEADERS).json()
    url = f"/test/payments/{confirmed['payment_id']}/reverse"
    assert provider.post(url, headers=API_HEADERS).status_code == 403
    reversal = provider.post(url, headers=TEST_HEADERS)
    assert reversal.json()["status"] == "REVERSED"
    assert provider.post(url, headers=TEST_HEADERS).status_code == 409
    provider.post(
        "/test/faults",
        json={"idempotency_key": "unknown-1", "mode": "unknown"},
        headers=TEST_HEADERS,
    )
    uncertain = provider.post("/v1/payments", json=payload("unknown-1"), headers=API_HEADERS).json()
    assert uncertain["status"] == "UNKNOWN"
    resolved = provider.post(
        f"/test/payments/{uncertain['payment_id']}/resolve",
        json={"status": "CONFIRMED"},
        headers=TEST_HEADERS,
    )
    assert resolved.json()["status"] == "CONFIRMED"


def test_before_create_failure_and_bad_response(provider: TestClient):
    provider.post(
        "/test/faults",
        json={"idempotency_key": "no-create", "mode": "fail_before_create"},
        headers=TEST_HEADERS,
    )
    assert (
        provider.post("/v1/payments", json=payload("no-create"), headers=API_HEADERS).status_code
        == 503
    )
    assert (
        provider.get("/v1/payments/by-idempotency-key/no-create", headers=API_HEADERS).status_code
        == 404
    )
    provider.delete("/test/faults/no-create", headers=TEST_HEADERS)
    assert (
        provider.post("/v1/payments", json=payload("no-create"), headers=API_HEADERS).json()[
            "status"
        ]
        == "CONFIRMED"
    )
    provider.post(
        "/test/faults",
        json={"idempotency_key": "bad-response", "mode": "unexpected_response"},
        headers=TEST_HEADERS,
    )
    surprising = provider.post("/v1/payments", json=payload("bad-response"), headers=API_HEADERS)
    assert surprising.json() == {"unexpected": "simulated_response"}
    assert (
        provider.get("/v1/payments/by-idempotency-key/bad-response", headers=API_HEADERS).json()[
            "status"
        ]
        == "CONFIRMED"
    )


def test_outage_blocks_lookup_and_can_be_cleared_with_test_token(provider: TestClient):
    first = provider.post("/v1/payments", json=payload(), headers=API_HEADERS).json()
    assert (
        provider.post("/test/outage", json={"unavailable": True}, headers=TEST_HEADERS).status_code
        == 204
    )
    assert provider.get("/health").status_code == 503
    assert (
        provider.get(f"/v1/payments/{first['payment_id']}", headers=API_HEADERS).status_code == 503
    )
    assert (
        provider.post("/test/outage", json={"unavailable": False}, headers=TEST_HEADERS).status_code
        == 204
    )
    assert (
        provider.get(f"/v1/payments/{first['payment_id']}", headers=API_HEADERS).json()["status"]
        == "CONFIRMED"
    )


@pytest.mark.parametrize("amount", ["0", "1.001", "-1", "1e3", "Infinity"])
def test_invalid_amounts_are_rejected(provider: TestClient, amount: str):
    response = provider.post("/v1/payments", json=payload(amount=amount), headers=API_HEADERS)
    assert response.status_code == 422


def test_bulk_seed_is_atomic_idempotent_and_test_only(provider: TestClient):
    batch = {
        "payments": [
            {**payload("base-1"), "status": "CONFIRMED"},
            {**payload("base-2", amount="45.50"), "status": "FAILED"},
        ]
    }
    assert provider.post("/test/seed-payments", json=batch, headers=API_HEADERS).status_code == 403
    first = provider.post("/test/seed-payments", json=batch, headers=TEST_HEADERS)
    assert first.json()["created"] == 2
    second = provider.post("/test/seed-payments", json=batch, headers=TEST_HEADERS)
    assert second.json()["replayed"] == 2
    changed = {
        "payments": [
            {**payload("base-3"), "status": "CONFIRMED"},
            {**payload("base-2", amount="46.50"), "status": "FAILED"},
        ]
    }
    assert (
        provider.post("/test/seed-payments", json=changed, headers=TEST_HEADERS).status_code == 409
    )
    assert (
        provider.get("/v1/payments/by-idempotency-key/base-3", headers=API_HEADERS).status_code
        == 404
    )


def test_production_hides_test_routes(tmp_path):
    settings = ProviderSettings(str(tmp_path / "prod.sqlite3"), "provider-secret")
    with TestClient(create_app(settings)) as client:
        response = client.post("/test/reset", headers=TEST_HEADERS)
        assert response.status_code == 404


def test_concurrent_duplicate_submission_is_single_durable_payment(tmp_path):
    path = str(tmp_path / "concurrent.sqlite3")
    ledger = SQLiteLedger(path)
    ledger.initialize()
    request = PaymentCreate.model_validate(payload("concurrent-1"))
    with ThreadPoolExecutor(max_workers=12) as workers:
        results = list(workers.map(lambda _: ledger.submit(request), range(24)))
    assert len({result[0]["payment_id"] for result in results}) == 1
    assert sum(not result[2] for result in results) == 1
    assert SQLiteLedger(path).get_by_key("concurrent-1")["status"] == "CONFIRMED"
    with pytest.raises(ProviderError) as error:
        ledger.submit(PaymentCreate.model_validate(payload("concurrent-1", amount="130.00")))
    assert error.value.status_code == 409
