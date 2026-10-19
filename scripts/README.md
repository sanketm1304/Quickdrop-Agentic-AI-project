# Synthetic bootstrap

The generator uses a fixed random seed and the versioned `app.policy` functions. At its default size, it produces 5,000 synthetic riders, 60,020 trips over 2026-10-16 and 2026-10-17, 10,000 earning periods, component-level entitlements, valid recorded deductions, and one base payout per period. It stores no real contact information or bank details.

`scripts/golden_manifest.json` defines 12 deterministic expected outcomes, including the Rs 200.00 automatic limit and Rs 200.01 review boundary. `scripts/dispute_scenarios.json` maps all 16 prompt dispute types to source records. Nine types are represented by golden cases; seven have their own HTTP message sequences and provider-ledger checks in `evals.run`. The base seed loads source records and baseline provider payments. An optional HTTP demo fixture below creates representative dispute rows without direct financial database writes.

## Start and seed

Set separate non-placeholder values for `POSTGRES_PASSWORD`, `APP_DATABASE_PASSWORD`, and `SEED_DATABASE_PASSWORD` in the ignored `.env` file. `db_setup` alone receives the PostgreSQL owner password and runs migrations and database grants. The API and worker connect as `quickdrop_app`; the on-demand seed service connects as `quickdrop_seed`.

After `docker compose up --build -d` completes `db_setup` and starts MockPaySwift, run:

```bash
docker compose run --rm seed
```

`DEMO_RIDER_PASSWORD` and `DEMO_EMPLOYEE_PASSWORD` must each contain at least 12 characters. Only golden riders R0001-R0012 receive rider login hashes. OPS001 is a reviewer and OPS002 is an administrator; neither password is embedded in source data. The seed uses application IDs as conflict keys, so rerunning it fills interrupted inserts without replacing existing records or decisions. MockPaySwift baseline payouts are sent over its authenticated test API in atomic batches of at most 1,000; QuickDrop never accesses provider storage.

## Evaluate the dispute fixtures

With `QUICKDROP_TEST_MODE=true`, `MOCKPAYSWIFT_TEST_MODE=true`, and the test token set, the evaluator can reset only test fixtures and run the 12 golden and seven catalog HTTP cases. This reset clears operational conversation and dispute history, so run it against a local disposable test deployment:

```bash
docker compose exec app python -m evals.run --base-url http://localhost:8000 --provider-url http://mockpayswift:8001 --reset-fixtures --output /tmp/quickdrop-eval.json
docker compose exec -T app cat /tmp/quickdrop-eval.json > evals/results/local.json
```

For PowerShell, replace the second command with `docker compose exec -T app cat /tmp/quickdrop-eval.json | Set-Content -LiteralPath evals/results/local.json -Encoding utf8`. The app container writes reports to a temporary filesystem, so export the JSON through `docker compose exec`. The latest saved local Compose report, [`compose-latest.json`](../evals/results/compose-latest.json), passed all 12 golden and seven catalog cases.

## Create representative demo disputes

After the source seed, run the optional fixture in offline agent mode:

```bash
docker compose exec app python -m scripts.seed_demo_disputes --base-url http://localhost:8000
```

It uses `/messages` with fixed IDs for three distinct golden riders: R0002 gets a provider-confirmed simulated incentive correction, R0011 appears in the employee approval queue, and R0006 gets a verified no-balance explanation. The script authenticates to the existing trace and provider read APIs to confirm the expected source baseline, case state, and correction ledger. It never resets data, approves a case, or submits a provider payment directly. Reruns reuse matching existing cases; if a rider has an unrelated conversation or unexpected provider transaction, the script stops instead of modifying it. Run the evaluator before this optional fixture when both are needed, because the evaluator's test reset clears conversation and dispute history.

For a quick source preview, run `python -m scripts.generate_data --riders 12`. Regenerate the checked-in golden manifest with `python -m scripts.generate_data --write-manifest scripts/golden_manifest.json`. For a database-only load during isolated development, pass `--skip-provider`; financial investigations remain uncertain until provider records are seeded. The async `seed_provider_from_db(settings)` helper restores baseline provider records after a guarded provider reset.

## Check concurrent automatic claims

The optional F03 smoke checks the once-per-day automatic-payment rule using two simultaneous eligible claims for R0004 against the Compose PostgreSQL database. It creates only workflow fixtures, then requires exactly one automatic authorization and one confirmed MockPaySwift correction; the second same-day claim must require employee approval. Run it in test mode on a clean source-seeded deployment, or after a guarded fixture reset, before creating other R0004 corrections:

```bash
docker compose exec -T app python -m scripts.smoke_concurrent_claims
```

This smoke modifies test dispute and payment state. Reset fixtures again before a fresh golden evaluation. A live Compose run passed with one automatic authorization, one confirmed INR 40.00 provider correction, and approval required for the second same-day claim.
