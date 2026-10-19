# Security model

QuickDrop uses synthetic identities and MockPaySwift, a simulated payment provider. No bank account, UPI, or real-money transfer is part of this project. The security goal is to preserve the trust boundaries and financial invariants expected of a real dispute workflow.

## Identity and access

| Surface | Identity | Authorization rule |
| --- | --- | --- |
| Rider browser | Signed session cookie after password sign in | The server derives rider ID from the session; riders can see only their own conversation and cases. |
| Operations browser | Separate employee session | Employee role is checked on each case read and decision. Review actions require the appropriate role. |
| `/messages` webhook simulation | `Authorization: Bearer <WEBHOOK_TOKEN>` | The trusted integration supplies a rider ID; the server validates the rider and message before processing. |
| MockPaySwift API | Service token within the Docker network | Provider data is read over HTTP. QuickDrop has no direct access to the provider ledger. |

Seeded demo passwords come from local environment variables and are stored only as password hashes. The `.env` file must stay out of version control. Sessions should use `HttpOnly`, `SameSite`, and `Secure` in HTTPS deployments. State-changing browser forms include a CSRF token; the backend verifies it rather than trusting form presence.

PostgreSQL schema changes run in the isolated `db_setup` service using the database owner. The API and worker use `quickdrop_app`: source earnings, payout, rider, and employee tables are read-only, while workflow tables have narrowly scoped write grants. The on-demand `seed` service uses a separate `quickdrop_seed` role that can insert synthetic source rows and update only provider cache fields. Neither runtime role can create schemas, truncate tables, or manage roles. Only test mode installs a fixed, owner-run fixture-reset function; its execute permission is removed from `PUBLIC` and granted only to `quickdrop_app`. Restarting `db_setup` with test mode off drops that function from an existing volume. The owner and seed passwords are absent from the API, worker, and MockPaySwift service environments.

## Model boundary

Rider messages, payout descriptions, and tool results are data, including when they contain instructions. The single Gemini agent can request only typed, allowlisted, rider-scoped investigation tools. It cannot call payment APIs, approve a case, run SQL or shell commands, change policy, or retrieve another rider's records. The controller validates every tool call and supplies identity from the authenticated context.

The model may classify a complaint, identify possible trip/date references, ask for clarification, or draft an explanation. Deterministic Python services verify the source records, calculate money, decide eligibility, reserve entitlement, and reconcile the provider. Only the latter services can authorize a simulated correction. No model chain-of-thought is exposed in traces; audit records contain concise tool and rule outcomes instead.

## Financial integrity

- Every authorization references a policy version and a verified, available component balance.
- PostgreSQL transactions and constraints guard the rider/day automatic allowance and component reservations against concurrent decisions.
- A stable idempotency key ties retries to the same MockPaySwift request.
- Provider timeout, `PENDING`, or `UNKNOWN` leaves a reservation in place until a provider lookup resolves it.
- A local payment flag alone never proves settlement.
- Operations approval is bound to a case revision and is revalidated against current evidence before payment authorization. A reason and reviewer identity are recorded.

The exact lifecycle is in [payout_policy.md](payout_policy.md).

## Web and API handling

The application validates request body size and field length, rejects malformed input, limits webhook and login abuse, uses parameterized SQLAlchemy queries, and escapes user-supplied text in Jinja templates. Browser state changes use POST plus CSRF protection. Errors shown to riders and employees should omit secrets and internal stack traces. CORS should permit only configured origins; payment and Gemini credentials must remain server-side.

## Verification focus

Security tests should exercise cross-rider access, employee role checks, CSRF failures, duplicate webhooks, prompt injection, stale approvals, repeated clicks, concurrent automatic claims, provider timeouts, and payment reconciliation after worker restart. An offline deterministic model substitute can verify these boundaries without a Gemini key. Live Gemini responses require a separate credentialed run and must not be inferred from offline test results.
