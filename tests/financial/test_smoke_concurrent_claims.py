"""Guard the seeded source assumptions used by the PostgreSQL F03 smoke."""

from decimal import Decimal

from scripts.generate_data import generate_period
from scripts.smoke_concurrent_claims import PERIOD_ID, TRIP_IDS


def test_f03_seed_has_two_distinct_auto_eligible_unpaid_components():
    period = generate_period(4, 2)
    assert period.period["id"] == PERIOD_ID
    assert period.provider_payment["status"] == "CONFIRMED"

    unpaid = {}
    for trip_id in TRIP_IDS:
        component = next(
            row for row in period.components if row["trip_id"] == trip_id and row["kind"] == "BASE"
        )
        paid = sum(
            (row["amount"] for row in period.allocations if row["component_id"] == component["id"]),
            Decimal(0),
        )
        unpaid[trip_id] = component["amount"] - paid
    assert unpaid == {"T0004201": Decimal("40.00"), "T0004202": Decimal("40.00")}
