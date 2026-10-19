# QuickDrop payout policy — version `QD-2026-01`

This policy applies only to synthetic QuickDrop records and simulated MockPaySwift transfers. It is the financial contract for the deterministic Python engine. Gemini may identify a rider's concern and request investigation tools; it cannot change this policy or authorize a transfer.

## Scope and units

- Currency: Indian rupees (INR).
- Money: Python `Decimal`, quantized to ₹0.01 with `ROUND_HALF_UP`. Never use binary floats in a calculation.
- Source distance: a nonnegative `Decimal` in kilometres, stored to 0.01 km. Source data is validated before calculation.
- Business timezone: `Asia/Kolkata`. Convert an aware timestamp to this timezone before selecting a business date. The date of authorization, rather than the earning date, determines the daily automatic correction allowance.
- Policy version: every calculation, dispute decision, allocation, and authorization records `QD-2026-01`. A future version must be explicit and must not recalculate historical decisions silently.

## Earning components

Only a completed eligible trip earns trip pay. A cancelled trip earns ₹0 under `QD-2026-01`.

| Component | `QD-2026-01` rule |
| --- | --- |
| Base pay | ₹40.00 per completed eligible trip |
| Distance pay | ₹8.00 × source distance in km |
| Surge | ₹0.00, ₹15.00, or ₹30.00 for source categories `NONE`, `STANDARD`, and `HIGH` |
| Daily incentive | ₹50.00 × `floor(completed eligible trips on a rider business day / 10)` |

The surge category comes from the trip source record. Neither the rider's message nor Gemini can set or infer it. Calculate trip components from the validated source distance and category, then quantize each monetary component to paise before summing. For example, a 3.25 km completed trip in the `STANDARD` surge category earns ₹40.00 + ₹26.00 + ₹15.00 = ₹81.00.

A daily incentive is attached to the relevant rider and local business date. Each earned ₹50.00 increment must have a stable component identity so two cases cannot compensate the same increment twice.

### Deductions

A deduction is valid only if an operational source record identifies the rider, a positive amount, an explicit policy category and reason, and an evidence reference. An unrecorded statement in chat is never a deduction. Invalid or unclear deduction records require human investigation; they are not silently included in expected earnings.

For an earning period:

```text
expected_earnings = completed_trip_base
                  + completed_trip_distance
                  + source_surge
                  + earned_daily_incentives
                  - valid_recorded_deductions
```

## Provider-confirmed payout and outstanding balance

MockPaySwift is the authority for settlement. A locally cached `PAID` or `CONFIRMED` field is only an observation until linked to a current provider-confirmed transaction. An accepted submission, `CREATED`, `PENDING`, `UNKNOWN`, or application `RECONCILING` status is not proof of payment.

The engine attributes confirmed base payouts and confirmed prior corrections to stable earning component IDs. For the components in the current dispute:

```text
verified_outstanding = max(0, expected_entitlement - provider_confirmed_paid)
reserved_amount = active, nonterminal allocations against that entitlement
available_to_authorize = max(0, verified_outstanding - reserved_amount)
authorized_amount <= available_to_authorize
```

The system stores the rider's `claimed_amount` separately. It is evidence of what the rider said, never proof of entitlement. Unknown attribution, conflicting evidence, or an uncertain provider result blocks automatic payment and goes to operations review. If the verified available balance is zero, the rider gets an explanation rather than another payment.

Allocation is exclusive for each earning component across active and confirmed authorizations. A period-level shortfall and a trip-level missing surge can describe the same source component; they must share its allocation record. Confirmed amounts remain allocated. A new correction or a second dispute must recalculate the available balance under transaction locks.

## Automatic simulated corrections

A case may receive an automatic correction only when all of these are true:

1. Rider identity and trip ownership are verified by the backend.
2. The earning components, payout attribution, and MockPaySwift settlement evidence are clear.
3. The amount for the disputed components is positive, available, and no more than ₹200.00. ₹200.00 qualifies; ₹200.01 requires a reviewer. The engine does not split a larger verified case into a ₹200.00 automatic payment.
4. No other automatic correction is reserved, pending, uncertain, or confirmed for that rider on the authorization business date.
5. No duplicate or overlapping allocation exists.
6. No fraud or conflicting-evidence flag requires human review.

Automatic authorization happens in a database transaction. The engine locks the rider's authorization scope, recalculates the available balance, records the authorization and exclusive component allocations, and reserves the rider/day allowance. External HTTP calls occur after commit, without holding that lock. MockPaySwift receives a stable idempotency key for retries.

### Daily allowance lifecycle

| Provider outcome | Component reservation | Rider/day automatic allowance |
| --- | --- | --- |
| Reserved, submitting, created, pending, unknown, or reconciling | Held | Held; another automatic correction is blocked |
| Provider-confirmed settlement | Permanently consumed | Permanently consumed for that business date |
| Authoritatively failed by idempotency-key lookup, or provider confirms no transaction was created | Released after recorded reconciliation | Released after recorded reconciliation |
| Reversed or conflicting result | Kept for review until a safe resolution is recorded | Blocked pending operations review |

A network timeout does not release a reservation. The worker looks up the existing idempotency key before any retry. Failed attempts and releases remain in the audit trail.

## Human decisions

Operations approval is required for a verified amount above ₹200.00, a repeat correction on the same rider/day, unclear or conflicting records, suspicious ownership, uncertain provider outcomes, and other policy exceptions. A reviewer must be authenticated and authorized, supply a reason, and act on a current case revision. Before accepting approval, the backend again checks ownership, provider settlement, component allocations, and the latest available balance under transaction locks. Approval may authorize only the verified available amount. Duplicate clicks and stale revisions must not create another authorization.

Rejection records the reviewer and reason and leaves no new payment authorization. Reconciliation or later rider corrections preserve historical decisions and transfers; they open a new investigation where necessary. A corrected claimed amount does not silently change a settled transfer.

## Audit evidence

For every decision, retain the policy version, source trip and incentive IDs, relevant payout and provider transaction references, expected-component calculations, confirmed paid amounts, active reservations, available balance, rule outcomes, human reason when applicable, stable payment idempotency key, and state transitions. Audit records are append-only from the application's perspective and must not contain model chain-of-thought.
