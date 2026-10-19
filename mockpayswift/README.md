# MockPaySwift

MockPaySwift is QuickDrop's fictional payment provider. It records **simulated**
INR transactions in a SQLite database separate from QuickDrop's PostgreSQL data.
It has no bank, UPI, or real payment connection.

## Configuration

| Variable | Purpose |
| --- | --- |
| `MOCKPAYSWIFT_DATABASE_PATH` | Durable SQLite file (default `./data/mockpayswift.sqlite3`) |
| `MOCKPAYSWIFT_API_KEY` | Required bearer token for all transaction APIs |
| `MOCKPAYSWIFT_TEST_MODE` | Set to `true` to enable guarded fixture and fault controls |
| `MOCKPAYSWIFT_TEST_TOKEN` | Additional `X-Test-Token` required by all `/test/*` routes |

Run with `uvicorn mockpayswift.main:app --host 0.0.0.0 --port 8001`.
`GET /health` is unauthenticated. All `/v1/*` calls require
`Authorization: Bearer <MOCKPAYSWIFT_API_KEY>`. A missing API key makes the
transaction API unavailable.

## Payment API

`POST /v1/payments` accepts:

```json
{
  "idempotency_key": "correction:D-001:v1",
  "rider_id": "R-001",
  "dispute_reference": "D-001",
  "amount": "120.00",
  "currency": "INR",
  "source_ref": "trip:T-001:surge",
  "earning_period": "2026-10-17"
}
```

`source_ref` and `earning_period` are optional attribution fields. The amount
must be a positive decimal **string** with at most two fractional digits. The
response carries a server-generated `payment_id`, a status, timestamps, and
`simulated: true`. A new payment returns 201. Repeating the same key and data
returns the same transaction with 200. Reusing a key with different data
returns 409. The provider persists the key check and transaction in one SQLite
write transaction, so concurrent retries cannot create multiple payments.

The default simulation settles immediately as `CONFIRMED`; a successful HTTP
submission in other modes can remain `PENDING` or `UNKNOWN`. Use these read APIs
to verify settlement:

- `GET /v1/payments/{payment_id}`
- `GET /v1/payments/by-idempotency-key/{key}`
- `GET /v1/riders/{rider_id}/transactions?limit=100&offset=0`
- `GET /v1/payments/{payment_id}/events`

The rider history response contains `items` and `next_offset`. Status changes
are preserved in the event ledger. A reversal is represented as `REVERSED` and
does not count as current settlement.

## Test and demo controls

`/test/*` routes require both the provider bearer token and `X-Test-Token`,
and return 404 when test mode is disabled. They are intended for synthetic
fixtures and resilience tests only:

- `POST /test/seed-payments`: 1–1000 payments per atomic batch, each with the
  normal payment fields and optional `status` (`CONFIRMED` by default). The
  response includes `created`, `replayed`, and `items` with stable payment IDs.
- `POST /test/faults`: install a fault with either `idempotency_key` or
  `idempotency_prefix`, plus `mode` and optional `delay_ms`. Prefix faults apply
  to the first matching new payment only, so an evaluator can target a generated
  authorization key. Exact-key faults take precedence. Modes are
  `fail_before_create`, `timeout_after_create`,
  `delayed`, `failed_settlement`, `unknown`, `unavailable`, and
  `unexpected_response`.
- `DELETE /test/faults/{key}`: remove an exact-key fault, or use
  `prefix:{value}` as the path key to remove a prefix fault.
- `POST /test/payments/{payment_id}/resolve`: set a created, pending, or unknown
  transaction to `PENDING`, `CONFIRMED`, `FAILED`, or `UNKNOWN` where valid.
- `POST /test/payments/{payment_id}/reverse`: reverse a confirmed transaction.
- `POST /test/outage`: set `{"unavailable": true}` to make the health and all
  transaction APIs return 503; test controls remain available to clear it.
- `POST /test/reset`: clear the simulated ledger and injected faults.

`timeout_after_create` commits a transaction before returning HTTP 504. Its
payment history records `simulated_timeout_after_create` as the event reason.
Reconciliation by idempotency key reveals its outcome, and retrying the exact
same submission returns the original transaction. `delayed` stays pending until
its configured deadline; the next lookup advances it to confirmed settlement.
