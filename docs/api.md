# HTTP interface

All payment amounts are INR strings or decimal values from server-side source records. A client cannot supply an amount to authorize. Browser requests use signed session cookies and CSRF tokens on POST forms.

## Webhook simulation

`POST /messages` accepts `Authorization: Bearer <WEBHOOK_TOKEN>` and JSON:

```json
{
  "message_id": "wamid.TEST001",
  "rider_id": "R0001",
  "text": "Bhai order T926334 ka surge nahi mila",
  "received_at": "2026-09-22T09:05:00+05:30"
}
```

Its response preserves `reply`:

```json
{"reply": "I have your message and am checking the payout."}
```

The actual reply is generated from case state. When work outlasts the request deadline, it is a truthful processing acknowledgement. Reusing a `message_id` must not create another dispute or payment.

`GET /trace/{rider_id}` returns a chronological, authorization-filtered decision trace. A rider may request only their own ID; authorized employees may inspect a rider. Traces show evidence IDs and policy outcomes, not hidden model reasoning.

`GET /ops/pending` returns cases needing review. It requires an authorized employee session.

## Browser routes

| Method and path | Purpose | Form fields or response |
| --- | --- | --- |
| `GET /login` | Rider sign in page | HTML |
| `POST /login` | Rider sign in | `rider_id`, `password`, `csrf_token` |
| `POST /logout` | Rider sign out | `csrf_token` |
| `GET /rider` | Conversation and case status | HTML, rider session required |
| `POST /rider/messages` | Send a rider message | `text`, `csrf_token`; regular form redirects, HTMX returns the thread fragment |
| `GET /rider/thread` | Refresh conversation | HTMX thread fragment, rider session required |
| `GET /rider/status` | Refresh case and payment statuses | HTMX case-card fragment, rider session required |
| `GET /ops/login` | Employee sign in page | HTML |
| `POST /ops/login` | Employee sign in | `employee_id`, `password`, `csrf_token` |
| `POST /ops/logout` | Employee sign out | `csrf_token` |
| `GET /ops` | Summary metrics, search, and case queue | Optional `q` and `status` query parameters |
| `GET /ops/cases/{id}` | Case evidence and audit timeline | HTML, authorized employee required |
| `POST /ops/cases/{id}/approve` | Approve a verified available balance | `reason`, `revision`, `csrf_token` |
| `POST /ops/cases/{id}/reject` | Reject a case | `reason`, `revision`, `csrf_token` |

Approval and rejection require a nonempty reviewer reason. The server checks role, CSRF token, case revision, provider state, and current available entitlement. A hidden revision field supports stale-page detection but is not trusted on its own.

## MockPaySwift

The provider service is reachable by the application and worker inside Docker. Its HTTP API supports submitting a payment with a stable idempotency key, looking up a transaction by payment ID or key, and listing rider transactions. It keeps its own persistent ledger and can simulate pending, failed, reversed, and uncertain outcomes. See the service's OpenAPI document in a local development deployment for its exact request and response schemas. Test-only failure controls require separate authorization.
