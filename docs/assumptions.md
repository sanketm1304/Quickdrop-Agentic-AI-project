# Project assumptions and boundaries

- QuickDrop, its riders, employees, trips, payout records, and MockPaySwift are fictional. Seeded contacts are synthetic identifiers, not messaging destinations.
- The application demonstrates a payout dispute process and financial safeguards. It never transfers real money and does not integrate with a bank, UPI, or a real payment provider.
- The WhatsApp-style `/messages` endpoint is a simulation. The browser can show the durable case state; proactive outbound messaging is outside the initial scope.
- The initial business timezone is `Asia/Kolkata`, independent of the host computer's timezone.
- `QD-2026-01` is the initial policy version. Historical calculations and decisions retain their version.
- MockPaySwift is the authority for whether a simulated transaction settled. A network acknowledgement is not settlement.
- Gemini is the natural-language interface and tool selector. If no Gemini key is configured, `AGENT_MODE=offline` provides deterministic behavior for local development and tests. Its wording and language coverage are not evidence of live Gemini performance.
- Existing rider and employee credentials are provisioned by the local seed process from `.env` values. Documentation does not contain reusable passwords.
- A browser may need access to the pinned Bootstrap and HTMX CDNs for the intended appearance and live polling. Standard HTML forms remain usable without HTMX; packaging local copies is a future offline improvement.
- Performance, model quality, and integration claims must be backed by executed tests or evaluation runs. No result is implied by the presence of a test or evaluator file.
