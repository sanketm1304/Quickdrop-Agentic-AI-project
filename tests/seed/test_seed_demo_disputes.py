"""Demo fixture must reject stale states and provider-ledger surprises."""

from decimal import Decimal

import pytest

from scripts.seed_demo_disputes import CASES, DemoError, _corrections, _is_ready


def baseline(case):
    return {
        "rider_id": case.rider_id,
        "idempotency_key": f"base:EP:{case.rider_id}:2026-10-17",
        "status": "CONFIRMED",
        "amount": str(case.baseline),
        "simulated": True,
    }


def opened(case, dispute_id="D1"):
    return {
        "event_type": "DISPUTE_OPENED",
        "dispute_id": dispute_id,
        "details": {"category": case.category, "earning_date": "2026-10-17"},
    }


def calculation(case, status, dispute_id="D1"):
    return {
        "event_type": "FINANCIAL_CALCULATION",
        "dispute_id": dispute_id,
        "details": {"verified_outstanding": str(case.outstanding), "case_status": status},
    }


def test_completed_case_needs_provider_confirmation_after_latest_calculation():
    case = CASES[0]
    correction = {
        "rider_id": case.rider_id,
        "idempotency_key": "QD-AUTH-one",
        "dispute_reference": "D1",
        "status": "CONFIRMED",
        "amount": str(case.outstanding),
        "simulated": True,
    }
    events = [
        opened(case),
        calculation(case, "AUTO_ELIGIBLE"),
        {
            "event_type": "PAYMENT_RECONCILED",
            "dispute_id": "D1",
            "details": {"local_status": "CONFIRMED"},
        },
    ]
    assert _is_ready(case, events, [baseline(case), correction], [])
    events.append(calculation(case, "UNDER_REVIEW"))
    assert not _is_ready(case, events, [baseline(case), correction], [])


def test_review_and_no_balance_require_current_case_state_without_correction():
    review, no_balance = CASES[1:]
    pending = [
        {
            "id": "D1",
            "rider_id": review.rider_id,
            "status": "PENDING_APPROVAL",
            "verified_outstanding": str(review.outstanding),
        }
    ]
    assert _is_ready(
        review,
        [opened(review), calculation(review, "PENDING_APPROVAL")],
        [baseline(review)],
        pending,
    )
    assert _is_ready(
        no_balance,
        [opened(no_balance), calculation(no_balance, "NO_BALANCE")],
        [baseline(no_balance)],
        [],
    )
    assert not _is_ready(
        no_balance,
        [opened(no_balance), calculation(no_balance, "UNDER_REVIEW")],
        [baseline(no_balance)],
        [],
    )


def test_provider_guard_rejects_duplicate_or_cross_rider_correction():
    case = CASES[0]
    correction = {
        "rider_id": case.rider_id,
        "idempotency_key": "QD-AUTH-one",
        "dispute_reference": "D1",
        "status": "CONFIRMED",
        "amount": str(Decimal("50.00")),
        "simulated": True,
    }
    with pytest.raises(DemoError, match="more than one correction"):
        _corrections(
            case,
            [baseline(case), correction, {**correction, "idempotency_key": "QD-AUTH-two"}],
        )
    with pytest.raises(DemoError, match="another rider"):
        _corrections(case, [baseline(case), {**correction, "rider_id": "R0999"}])
