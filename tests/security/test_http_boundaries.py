"""Authenticated HTTP boundaries must not trust rider or reviewer input."""

import os
from datetime import UTC, datetime

import httpx
import pytest

os.environ.setdefault("SESSION_SECRET", "test-only-session-secret-32-characters-long")
os.environ.setdefault("WEBHOOK_TOKEN", "test-only-webhook-token")
os.environ.setdefault("MOCKPAYSWIFT_API_KEY", "test-only-provider-token")

from app import main
from app.security import new_session


@pytest.mark.asyncio
async def test_missing_authentication_blocks_webhook_and_operations() -> None:
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {
            "message_id": "security-M1",
            "rider_id": "R1",
            "text": "Check my payout",
            "received_at": datetime.now(UTC).isoformat(),
        }
        assert (await client.post("/messages", json=payload)).status_code == 401
        assert (await client.get("/ops/pending")).status_code == 401
        assert (await client.get("/trace/R1")).status_code == 403


@pytest.mark.asyncio
async def test_rider_identity_cannot_be_spoofed_or_used_for_approval(monkeypatch) -> None:
    accepted: list[dict] = []

    async def capture_enqueue(**kwargs):
        accepted.append(kwargs)
        return "Message saved"

    monkeypatch.setattr(main, "enqueue_message", capture_enqueue)
    signed, csrf = new_session(main.settings, "R1", "rider")
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        client.cookies.set("qd_session", signed)
        response = await client.post(
            "/rider/messages",
            data={"text": "Check my payout", "csrf_token": csrf, "rider_id": "R2"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert accepted[0]["rider_id"] == "R1"
        response = await client.post(
            "/ops/cases/D1/approve",
            data={"reason": "Please approve this claim", "revision": 1, "csrf_token": csrf},
        )
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_oversized_streaming_body_is_rejected_before_webhook(monkeypatch) -> None:
    accepted = False

    async def capture_enqueue(**_kwargs):
        nonlocal accepted
        accepted = True
        return "Message saved"

    async def body_chunks():
        yield b"x" * 10_000
        yield b"x" * 10_000

    monkeypatch.setattr(main, "enqueue_message", capture_enqueue)
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/messages",
            content=body_chunks(),
            headers={"Authorization": f"Bearer {main.settings.webhook_token}"},
        )
    assert response.status_code == 413
    assert not accepted
