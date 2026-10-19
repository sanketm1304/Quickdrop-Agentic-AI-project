"""SQLite-backed provider ledger. This service never calls a real payment rail."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from .models import FaultCreate, PaymentCreate, SeedPayment

PREFIX_FAULT_MARKER = "/prefix/"


class ProviderError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime | None = None) -> str:
    return (moment or _now()).isoformat(timespec="microseconds")


class SQLiteLedger:
    """Independent, durable transaction ledger with atomic idempotency."""

    def __init__(self, path: str):
        self.path = path

    def initialize(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            if self.path != ":memory:":
                conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS payments (
                    payment_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payload_hash TEXT NOT NULL,
                    rider_id TEXT NOT NULL,
                    dispute_reference TEXT NOT NULL,
                    source_ref TEXT,
                    earning_period TEXT,
                    amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
                    currency TEXT NOT NULL CHECK (currency = 'INR'),
                    status TEXT NOT NULL CHECK (
                        status IN ('CREATED','PENDING','CONFIRMED','FAILED','REVERSED','UNKNOWN')
                    ),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    settled_at TEXT,
                    due_at TEXT,
                    settle_target TEXT CHECK (settle_target IN ('CONFIRMED','FAILED'))
                );
                CREATE INDEX IF NOT EXISTS ix_payments_rider
                    ON payments(rider_id, created_at, payment_id);
                CREATE INDEX IF NOT EXISTS ix_payments_due
                    ON payments(status, due_at);
                CREATE TABLE IF NOT EXISTS status_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payment_id TEXT NOT NULL REFERENCES payments(payment_id),
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_events_payment
                    ON status_events(payment_id, event_id);
                CREATE TABLE IF NOT EXISTS test_faults (
                    idempotency_key TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    delay_ms INTEGER NOT NULL CHECK (delay_ms >= 0)
                );
                CREATE TABLE IF NOT EXISTS test_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("PRAGMA synchronous = FULL")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    @staticmethod
    def _fingerprint(request: PaymentCreate) -> str:
        fields = request.model_dump(
            include={
                "idempotency_key",
                "rider_id",
                "dispute_reference",
                "amount",
                "currency",
                "source_ref",
                "earning_period",
            }
        )
        return hashlib.sha256(
            json.dumps(fields, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _view(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "payment_id": row["payment_id"],
            "idempotency_key": row["idempotency_key"],
            "rider_id": row["rider_id"],
            "dispute_reference": row["dispute_reference"],
            "source_ref": row["source_ref"],
            "earning_period": row["earning_period"],
            "amount": f"{Decimal(row['amount_minor']) / 100:.2f}",
            "currency": row["currency"],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "settled_at": row["settled_at"],
            "simulated": True,
        }

    @staticmethod
    def _get_row(conn: sqlite3.Connection, column: str, value: str) -> sqlite3.Row | None:
        # Only internal constant column names are passed here.
        return conn.execute(f"SELECT * FROM payments WHERE {column} = ?", (value,)).fetchone()

    def _transition(
        self,
        conn: sqlite3.Connection,
        payment_id: str,
        status: str,
        reason: str,
        *,
        due_at: str | None = None,
        settle_target: str | None = None,
    ) -> None:
        row = self._get_row(conn, "payment_id", payment_id)
        assert row is not None
        previous = row["status"]
        if previous == status:
            return
        allowed = {
            "CREATED": {"PENDING", "CONFIRMED", "FAILED", "UNKNOWN"},
            "PENDING": {"CONFIRMED", "FAILED", "UNKNOWN"},
            "UNKNOWN": {"CONFIRMED", "FAILED", "PENDING"},
            "CONFIRMED": {"REVERSED"},
            "FAILED": set(),
            "REVERSED": set(),
        }
        if status not in allowed[previous]:
            raise ProviderError(409, f"invalid payment transition: {previous} to {status}")
        at = _stamp()
        settled_at = at if status == "CONFIRMED" else row["settled_at"]
        conn.execute(
            """UPDATE payments SET status=?, updated_at=?, settled_at=?, due_at=?,
               settle_target=? WHERE payment_id=?""",
            (status, at, settled_at, due_at, settle_target, payment_id),
        )
        conn.execute(
            """INSERT INTO status_events
               (payment_id, from_status, to_status, reason, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (payment_id, previous, status, reason, at),
        )

    def _insert(
        self,
        conn: sqlite3.Connection,
        request: PaymentCreate,
        *,
        status: str,
        reason: str,
        delay_ms: int = 0,
        settle_target: str = "CONFIRMED",
    ) -> dict[str, Any]:
        payment_id = "mps_" + uuid.uuid4().hex
        created_at = _stamp()
        amount_minor = int(Decimal(request.amount) * 100)
        conn.execute(
            """INSERT INTO payments
               (payment_id,idempotency_key,payload_hash,rider_id,dispute_reference,
                source_ref,earning_period,amount_minor,currency,status,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                payment_id,
                request.idempotency_key,
                self._fingerprint(request),
                request.rider_id,
                request.dispute_reference,
                request.source_ref,
                request.earning_period,
                amount_minor,
                request.currency,
                "CREATED",
                created_at,
                created_at,
            ),
        )
        conn.execute(
            """INSERT INTO status_events
               (payment_id,from_status,to_status,reason,created_at)
               VALUES (?,NULL,'CREATED',?,?)""",
            (payment_id, reason, created_at),
        )
        if status == "CREATED":
            pass
        elif status == "REVERSED":
            self._transition(conn, payment_id, "PENDING", reason)
            self._transition(conn, payment_id, "CONFIRMED", reason)
            self._transition(conn, payment_id, "REVERSED", reason)
        elif status == "CONFIRMED":
            self._transition(conn, payment_id, "PENDING", reason)
            self._transition(conn, payment_id, "CONFIRMED", reason)
        elif status == "FAILED":
            self._transition(conn, payment_id, "PENDING", reason)
            self._transition(conn, payment_id, "FAILED", reason)
        elif status == "PENDING":
            due_at = _stamp(_now() + timedelta(milliseconds=delay_ms)) if delay_ms else None
            self._transition(
                conn,
                payment_id,
                "PENDING",
                reason,
                due_at=due_at,
                settle_target=settle_target if due_at else None,
            )
        elif status == "UNKNOWN":
            self._transition(conn, payment_id, "UNKNOWN", reason)
        else:
            raise ValueError(f"unsupported initial status: {status}")
        row = self._get_row(conn, "payment_id", payment_id)
        assert row is not None
        return self._view(row)

    def _advance_due(self, conn: sqlite3.Connection) -> None:
        due = conn.execute(
            """SELECT payment_id, settle_target FROM payments
               WHERE status='PENDING' AND due_at IS NOT NULL AND due_at <= ?""",
            (_stamp(),),
        ).fetchall()
        for row in due:
            self._transition(conn, row["payment_id"], row["settle_target"], "scheduled_settlement")

    def _settle_due(self) -> None:
        """Only acquire a write lock when a scheduled payment is ready."""
        with self._connect() as conn:
            due = conn.execute(
                """SELECT 1 FROM payments WHERE status='PENDING'
                   AND due_at IS NOT NULL AND due_at <= ? LIMIT 1""",
                (_stamp(),),
            ).fetchone()
        if due:
            with self._write() as conn:
                self._advance_due(conn)

    def submit(self, request: PaymentCreate) -> tuple[dict[str, Any], str | None, bool]:
        """Return (payment, injected_mode, replayed). A timeout is raised by the API after commit."""
        with self._write() as conn:
            self._advance_due(conn)
            existing = self._get_row(conn, "idempotency_key", request.idempotency_key)
            if existing:
                if existing["payload_hash"] != self._fingerprint(request):
                    raise ProviderError(
                        409, "idempotency key already used for different payment data"
                    )
                return self._view(existing), None, True
            fault = conn.execute(
                "SELECT mode,delay_ms FROM test_faults WHERE idempotency_key=?",
                (request.idempotency_key,),
            ).fetchone()
            if fault is None:
                prefix_faults = conn.execute(
                    "SELECT idempotency_key,mode,delay_ms FROM test_faults "
                    "WHERE idempotency_key LIKE '/prefix/%'"
                ).fetchall()
                matches = [
                    row
                    for row in prefix_faults
                    if request.idempotency_key.startswith(
                        row["idempotency_key"][len(PREFIX_FAULT_MARKER) :]
                    )
                ]
                if matches:
                    fault = max(matches, key=lambda row: len(row["idempotency_key"]))
                    # Prefix faults apply once, atomically with the matching creation.
                    conn.execute(
                        "DELETE FROM test_faults WHERE idempotency_key=?",
                        (fault["idempotency_key"],),
                    )
            mode = fault["mode"] if fault else None
            delay_ms = fault["delay_ms"] if fault else 0
            if mode in {"fail_before_create", "unavailable"}:
                raise ProviderError(503, "simulated provider unavailable before creation")
            if mode == "delayed":
                view = self._insert(
                    conn,
                    request,
                    status="PENDING",
                    reason="simulated_delayed_settlement",
                    delay_ms=max(delay_ms, 1),
                )
            elif mode == "failed_settlement":
                if delay_ms:
                    view = self._insert(
                        conn,
                        request,
                        status="PENDING",
                        reason="simulated_failed_settlement",
                        delay_ms=delay_ms,
                        settle_target="FAILED",
                    )
                else:
                    view = self._insert(
                        conn, request, status="FAILED", reason="simulated_failed_settlement"
                    )
            elif mode == "unknown":
                view = self._insert(conn, request, status="UNKNOWN", reason="simulated_unknown")
            else:
                reason = (
                    "simulated_timeout_after_create"
                    if mode == "timeout_after_create"
                    else "simulated_payment"
                )
                view = self._insert(conn, request, status="CONFIRMED", reason=reason)
            return view, mode, False

    def get_by_id(self, payment_id: str) -> dict[str, Any]:
        self._settle_due()
        with self._connect() as conn:
            row = self._get_row(conn, "payment_id", payment_id)
            if row is None:
                raise ProviderError(404, "payment not found")
            return self._view(row)

    def get_by_key(self, key: str) -> dict[str, Any]:
        self._settle_due()
        with self._connect() as conn:
            row = self._get_row(conn, "idempotency_key", key)
            if row is None:
                raise ProviderError(404, "payment not found")
            return self._view(row)

    def list_rider(self, rider_id: str, *, limit: int, offset: int) -> dict[str, Any]:
        self._settle_due()
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT * FROM payments WHERE rider_id=?
                   ORDER BY created_at, payment_id LIMIT ? OFFSET ?""",
                (rider_id, limit + 1, offset),
            ).fetchall()
            has_more = len(rows) > limit
            return {
                "items": [self._view(row) for row in rows[:limit]],
                "next_offset": offset + limit if has_more else None,
            }

    def history(self, payment_id: str) -> list[dict[str, Any]]:
        self._settle_due()
        with self._connect() as conn:
            if self._get_row(conn, "payment_id", payment_id) is None:
                raise ProviderError(404, "payment not found")
            rows = conn.execute(
                """SELECT from_status,to_status,reason,created_at FROM status_events
                   WHERE payment_id=? ORDER BY event_id""",
                (payment_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def configure_fault(self, fault: FaultCreate) -> None:
        key = (
            PREFIX_FAULT_MARKER + fault.idempotency_prefix
            if fault.idempotency_prefix is not None
            else fault.idempotency_key
        )
        assert key is not None
        with self._write() as conn:
            if (
                fault.idempotency_key
                and self._get_row(conn, "idempotency_key", fault.idempotency_key) is not None
            ):
                raise ProviderError(409, "payment already exists for idempotency key")
            conn.execute(
                """INSERT INTO test_faults(idempotency_key,mode,delay_ms) VALUES (?,?,?)
                   ON CONFLICT(idempotency_key) DO UPDATE SET mode=excluded.mode,
                   delay_ms=excluded.delay_ms""",
                (key, fault.mode, fault.delay_ms),
            )

    def clear_fault(self, key: str) -> None:
        stored_key = (
            PREFIX_FAULT_MARKER + key.removeprefix("prefix:") if key.startswith("prefix:") else key
        )
        with self._write() as conn:
            conn.execute("DELETE FROM test_faults WHERE idempotency_key=?", (stored_key,))

    def resolve(self, payment_id: str, status: str) -> dict[str, Any]:
        with self._write() as conn:
            row = self._get_row(conn, "payment_id", payment_id)
            if row is None:
                raise ProviderError(404, "payment not found")
            if row["status"] not in {"CREATED", "PENDING", "UNKNOWN"}:
                if row["status"] == status:
                    return self._view(row)
                raise ProviderError(
                    409, "only created, pending or unknown payments can be resolved"
                )
            self._transition(conn, payment_id, status, "test_resolution")
            row = self._get_row(conn, "payment_id", payment_id)
            assert row is not None
            return self._view(row)

    def reverse(self, payment_id: str) -> dict[str, Any]:
        with self._write() as conn:
            row = self._get_row(conn, "payment_id", payment_id)
            if row is None:
                raise ProviderError(404, "payment not found")
            if row["status"] != "CONFIRMED":
                raise ProviderError(409, "only confirmed payments can be reversed")
            self._transition(conn, payment_id, "REVERSED", "simulated_reversal")
            row = self._get_row(conn, "payment_id", payment_id)
            assert row is not None
            return self._view(row)

    def seed_batch(self, payments: list[SeedPayment]) -> dict[str, Any]:
        created = replayed = 0
        items: list[dict[str, str]] = []
        with self._write() as conn:
            for request in payments:
                existing = self._get_row(conn, "idempotency_key", request.idempotency_key)
                if existing:
                    if existing["payload_hash"] != self._fingerprint(request):
                        raise ProviderError(
                            409, "idempotency key already used for different payment data"
                        )
                    items.append(
                        {
                            "idempotency_key": existing["idempotency_key"],
                            "payment_id": existing["payment_id"],
                            "status": existing["status"],
                        }
                    )
                    replayed += 1
                    continue
                payment = self._insert(
                    conn, request, status=request.status, reason="synthetic_seed"
                )
                items.append(
                    {
                        "idempotency_key": payment["idempotency_key"],
                        "payment_id": payment["payment_id"],
                        "status": payment["status"],
                    }
                )
                created += 1
        return {"created": created, "replayed": replayed, "items": items}

    def reset(self) -> None:
        with self._write() as conn:
            conn.execute("DELETE FROM status_events")
            conn.execute("DELETE FROM payments")
            conn.execute("DELETE FROM test_faults")
            conn.execute("DELETE FROM test_state")

    def set_outage(self, unavailable: bool) -> None:
        with self._write() as conn:
            conn.execute(
                """INSERT INTO test_state(key,value) VALUES ('outage',?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                ("1" if unavailable else "0",),
            )

    def is_outage(self) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT value FROM test_state WHERE key='outage'").fetchone()
            return bool(row and row["value"] == "1")

    def check(self) -> bool:
        with self._connect() as conn:
            return conn.execute("SELECT 1").fetchone()[0] == 1
