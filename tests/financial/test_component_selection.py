"""Source component selection must respect the scope of a rider's claim."""

from decimal import Decimal

import pytest

from app.models import EarningComponent
from app.policy import money
from app.services.financial import Verification, select_component_allocations
from scripts.generate_data import generate_period


def _r0004_source() -> tuple[list[EarningComponent], Verification]:
    plan = generate_period(4, 2)
    paid_by_component = {row["component_id"]: row["amount"] for row in plan.allocations}
    components = [EarningComponent(**row) for row in plan.components]
    component_available = {
        row["id"]: money(row["amount"] - paid_by_component.get(row["id"], Decimal(0)))
        for row in plan.components
    }
    available = money(sum(component_available.values(), Decimal(0)))
    verification = Verification(
        period_id=plan.period["id"],
        expected=plan.period["expected_amount"],
        confirmed_paid=plan.payout["amount"],
        reserved=money(0),
        verified_outstanding=available,
        available=available,
        component_available=component_available,
        uncertain=False,
        reason=None,
        evidence=[],
    )
    assert available == Decimal("334.48")
    return components, verification


@pytest.mark.parametrize("category", ["TOTAL", "PARTIAL", "PAYOUT"])
@pytest.mark.parametrize(
    ("trip_id", "expected"),
    [
        ("T0004201", Decimal("137.00")),
        ("T0004202", Decimal("97.44")),
        ("T0004203", Decimal("100.04")),
    ],
)
def test_trip_claim_selects_only_that_trips_unpaid_components(
    category: str, trip_id: str, expected: Decimal
) -> None:
    components, verification = _r0004_source()

    selected = select_component_allocations(components, verification, category, trip_id)

    assert sum(selected.values(), Decimal(0)) == expected
    assert selected
    assert {component.trip_id for component in components if component.id in selected} == {trip_id}


def test_date_level_total_claim_selects_entire_period_balance() -> None:
    components, verification = _r0004_source()

    selected = select_component_allocations(components, verification, "TOTAL", None)

    assert sum(selected.values(), Decimal(0)) == Decimal("334.48")
    assert {component.trip_id for component in components if component.id in selected} >= {
        "T0004201",
        "T0004202",
        "T0004203",
    }
