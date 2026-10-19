"""Local demo authentication, signed sessions, and request safeguards."""

import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import Settings


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("password must contain at least 12 characters")
    salt = os.urandom(16)
    derived = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt$16384${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algorithm, cost, salt, expected = stored.split("$", 3)
        if algorithm != "scrypt" or cost != "16384":
            return False
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (ValueError, TypeError):
        return False


def session_serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.session_secret, salt="quickdrop-session-v1")


def new_session(settings: Settings, identity: str, role: str) -> tuple[str, str]:
    csrf = secrets.token_urlsafe(24)
    return session_serializer(settings).dumps({"id": identity, "role": role, "csrf": csrf}), csrf


def new_login_challenge(settings: Settings) -> tuple[str, str]:
    csrf = secrets.token_urlsafe(24)
    signed = session_serializer(settings).dumps({"csrf": csrf, "purpose": "login"})
    return signed, csrf


def require_login_csrf(request: Request, settings: Settings, submitted: str) -> None:
    signed = request.cookies.get("qd_login")
    if not signed or not submitted:
        raise HTTPException(status_code=403, detail="Invalid login CSRF token")
    try:
        data = session_serializer(settings).loads(signed, max_age=30 * 60)
    except (BadSignature, SignatureExpired) as exc:
        raise HTTPException(status_code=403, detail="Invalid login CSRF token") from exc
    if data.get("purpose") != "login" or not hmac.compare_digest(data.get("csrf", ""), submitted):
        raise HTTPException(status_code=403, detail="Invalid login CSRF token")


def read_session(request: Request, settings: Settings) -> dict | None:
    cookie = request.cookies.get("qd_session")
    if not cookie:
        return None
    try:
        data = session_serializer(settings).loads(cookie, max_age=8 * 3600)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict) or not all(data.get(key) for key in ("id", "role", "csrf")):
        return None
    return data


def require_identity(request: Request, settings: Settings, role: str) -> dict:
    session = read_session(request, settings)
    if not session or (session["role"] != role and session["role"] != "admin"):
        raise HTTPException(status_code=401, detail="Authentication required")
    return session


def require_csrf(request: Request, session: dict, submitted: str) -> None:
    if not submitted or not hmac.compare_digest(session["csrf"], submitted):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def require_bearer(request: Request, expected: str) -> None:
    provided = request.headers.get("authorization", "")
    if not hmac.compare_digest(provided, f"Bearer {expected}"):
        raise HTTPException(status_code=401, detail="Authentication required")


class RateLimiter:
    """Small per-process guard for local demo; reverse proxy limits are needed at scale."""

    def __init__(self, count: int, period_seconds: int):
        self.count = count
        self.period_seconds = period_seconds
        self.events: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> None:
        current = time.monotonic()
        events = self.events[key]
        while events and current - events[0] > self.period_seconds:
            events.popleft()
        if len(events) >= self.count:
            raise HTTPException(status_code=429, detail="Too many requests")
        events.append(current)
