"""Exercise the seeded human approval through HTTP after a golden evaluation run.

This creates one simulated correction for R0004. Reset fixtures before another
evaluation run; the script deliberately never resets data by itself.
"""

from __future__ import annotations

import argparse
import asyncio
import time

import httpx

from app.config import get_settings
from evals.run import _CsrfParser


def _csrf(html: str) -> str:
    parser = _CsrfParser()
    parser.feed(html)
    if not parser.value:
        raise AssertionError("CSRF token missing from page")
    return parser.value


async def smoke(base_url: str, provider_url: str) -> None:
    settings = get_settings()
    async with (
        httpx.AsyncClient(base_url=base_url, timeout=30) as app,
        httpx.AsyncClient(
            base_url=provider_url,
            headers={"Authorization": f"Bearer {settings.mockpayswift_api_key}"},
            timeout=30,
        ) as provider,
    ):
        login_page = await app.get("/ops/login")
        login_page.raise_for_status()
        login = await app.post(
            "/ops/login",
            data={
                "employee_id": "OPS001",
                "password": settings.demo_employee_password,
                "csrf_token": _csrf(login_page.text),
            },
        )
        assert login.status_code == 303 and "qd_session" in app.cookies, (
            f"employee login failed: {login.status_code}"
        )
        pending = await app.get("/ops/pending")
        pending.raise_for_status()
        case = next((row for row in pending.json()["items"] if row["rider_id"] == "R0004"), None)
        assert case and case["status"] == "PENDING_APPROVAL", "R0004 is not pending"
        case_page = await app.get(f"/ops/cases/{case['id']}")
        case_page.raise_for_status()
        decision = {
            "reason": "Verified simulated payout gap against source and provider records.",
            "revision": case["revision"],
            "csrf_token": _csrf(case_page.text),
        }
        approve = await app.post(f"/ops/cases/{case['id']}/approve", data=decision)
        assert approve.status_code == 303, (
            f"approval failed: {approve.status_code} {approve.text[:200]}"
        )
        duplicate = await app.post(f"/ops/cases/{case['id']}/approve", data=decision)
        assert duplicate.status_code == 409, f"duplicate approval returned {duplicate.status_code}"

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            response = await provider.get("/v1/riders/R0004/transactions", params={"limit": 50})
            response.raise_for_status()
            corrections = [
                item for item in response.json()["items"] if item["dispute_reference"] == case["id"]
            ]
            if len(corrections) == 1 and corrections[0]["status"] == "CONFIRMED":
                assert corrections[0]["amount"] == "334.48", corrections[0]
                print(
                    "Human approval: PASS; duplicate blocked; one INR 334.48 simulated payment confirmed"
                )
                return
            await asyncio.sleep(0.5)
        raise AssertionError("approved correction was not confirmed within 30 seconds")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--provider-url", default="http://127.0.0.1:8001")
    args = parser.parse_args()
    asyncio.run(smoke(args.base_url, args.provider_url))


if __name__ == "__main__":
    main()
