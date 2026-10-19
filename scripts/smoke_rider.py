"""Check the seeded rider's browser login, message form, and status panel."""

from __future__ import annotations

import argparse
import asyncio

import httpx

from app.config import get_settings
from evals.run import _CsrfParser


def _csrf(html: str) -> str:
    parser = _CsrfParser()
    parser.feed(html)
    if not parser.value:
        raise AssertionError("CSRF token missing from page")
    return parser.value


async def smoke(base_url: str) -> None:
    settings = get_settings()
    async with httpx.AsyncClient(base_url=base_url, timeout=30) as app:
        page = await app.get("/login")
        page.raise_for_status()
        login = await app.post(
            "/login",
            data={
                "rider_id": "R0003",
                "password": settings.demo_rider_password,
                "csrf_token": _csrf(page.text),
            },
        )
        assert login.status_code == 303 and "qd_session" in app.cookies, (
            f"rider login failed: {login.status_code}"
        )
        rider_page = await app.get("/rider")
        rider_page.raise_for_status()
        message = "Please check my payout status for 2026-10-17"
        sent = await app.post(
            "/rider/messages",
            data={"text": message, "csrf_token": _csrf(rider_page.text)},
        )
        assert sent.status_code == 303, f"message form returned {sent.status_code}"
        thread = await app.get("/rider/thread")
        thread.raise_for_status()
        assert message in thread.text, "rider message is missing from the conversation"
        status = await app.get("/rider/status")
        status.raise_for_status()
        print("Rider portal: PASS; login, CSRF-protected message form, thread, and status panel")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    asyncio.run(smoke(args.base_url))


if __name__ == "__main__":
    main()
