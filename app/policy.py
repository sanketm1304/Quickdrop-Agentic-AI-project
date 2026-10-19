from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

POLICY_VERSION = "QD-2026-01"
BUSINESS_TIMEZONE = ZoneInfo("Asia/Kolkata")
PAISE = Decimal("0.01")
AUTO_LIMIT = Decimal("200.00")
SURGE = {"NONE": Decimal(0), "STANDARD": Decimal(15), "HIGH": Decimal(30)}


def money(value: Decimal | str | int) -> Decimal:
    return Decimal(str(value)).quantize(PAISE, rounding=ROUND_HALF_UP)


def business_date(at: datetime) -> date:
    if at.tzinfo is None:
        raise ValueError("timezone-aware timestamp required")
    return at.astimezone(BUSINESS_TIMEZONE).date()


@dataclass(frozen=True)
class TripEarnings:
    base: Decimal
    distance: Decimal
    surge: Decimal

    @property
    def total(self) -> Decimal:
        return money(self.base + self.distance + self.surge)


def trip_earnings(distance_km: Decimal, status: str, surge_category: str) -> TripEarnings:
    if distance_km < 0:
        raise ValueError("distance cannot be negative")
    if surge_category not in SURGE:
        raise ValueError("unknown source surge category")
    if status == "CANCELLED":
        return TripEarnings(money(0), money(0), money(0))
    if status != "COMPLETED":
        raise ValueError("unknown trip status")
    return TripEarnings(money(40), money(Decimal(8) * distance_km), money(SURGE[surge_category]))


def daily_incentive(completed_count: int) -> Decimal:
    if completed_count < 0:
        raise ValueError("trip count cannot be negative")
    return money(50 * (completed_count // 10))


def expected_earnings(
    trips: list[TripEarnings], incentive: Decimal, deductions: Decimal
) -> Decimal:
    if deductions < 0:
        raise ValueError("deductions cannot be negative")
    return money(sum((trip.total for trip in trips), Decimal(0)) + incentive - deductions)


def available_to_authorize(
    entitlement: Decimal, provider_confirmed: Decimal, active_reservations: Decimal
) -> Decimal:
    return max(money(0), money(entitlement - provider_confirmed - active_reservations))


def requires_human(amount: Decimal, used_auto_today: bool, uncertain: bool) -> bool:
    return uncertain or used_auto_today or amount > AUTO_LIMIT
