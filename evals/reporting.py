"""Aggregate only observations collected during an HTTP evaluation run."""

from __future__ import annotations

from datetime import UTC, datetime
from math import ceil
from typing import Any


def _ratio(values: list[bool | None]) -> dict[str, Any]:
    measured = [value for value in values if value is not None]
    passed = sum(measured)
    return {
        "passed": passed,
        "evaluated": len(measured),
        "failed": len(measured) - passed,
        "unavailable": len(values) - len(measured),
        "accuracy": round(passed / len(measured), 4) if measured else None,
    }


def _latency(values: list[float]) -> dict[str, Any]:
    ordered = sorted(values)
    if not ordered:
        return {"samples": 0, "min_ms": None, "p50_ms": None, "p95_ms": None, "max_ms": None}
    return {
        "samples": len(ordered),
        "min_ms": round(ordered[0], 2),
        "p50_ms": round(ordered[ceil(len(ordered) * 0.50) - 1], 2),
        "p95_ms": round(ordered[ceil(len(ordered) * 0.95) - 1], 2),
        "max_ms": round(ordered[-1], 2),
    }


def build_report(
    *,
    cases: list[dict[str, Any]],
    checks: dict[str, list[bool | None]],
    webhook_latencies_ms: list[float],
    resolution_latencies_ms: list[float],
    tool_calls: int,
    successful_tool_calls: int,
    metadata: dict[str, Any],
    errors: list[str],
) -> dict[str, Any]:
    metrics = {name: _ratio(values) for name, values in sorted(checks.items())}
    metrics["agent_tool_call_reliability"] = _ratio(
        [True] * successful_tool_calls + [False] * (tool_calls - successful_tool_calls)
    )
    metrics["webhook_response_latency"] = _latency(webhook_latencies_ms)
    metrics["end_to_end_resolution_latency"] = _latency(resolution_latencies_ms)
    failed_checks = sum(metric.get("failed", 0) for metric in metrics.values())
    unavailable_checks = sum(metric.get("unavailable", 0) for metric in metrics.values())
    status = "FAIL" if failed_checks or errors else "INCOMPLETE" if unavailable_checks else "PASS"
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "status": status,
        "metadata": metadata,
        "metrics": metrics,
        "cases": cases,
        "errors": errors,
        "note": "Metrics come from this run's HTTP responses and provider ledger; wording is not exact-matched.",
    }
