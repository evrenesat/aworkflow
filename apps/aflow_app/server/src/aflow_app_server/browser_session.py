"""Signed rolling browser sessions for the web dashboard.

The session cookie never contains the deployment bearer token. It carries a
versioned payload with a random nonce and an integer expiry, signed with an
HMAC-SHA256 key that is domain-separated from the current deployment token so
token rotation immediately invalidates every issued session.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass

SESSION_COOKIE_NAME = "aflow_session"
SESSION_VERSION = 1
SESSION_MAX_AGE_SECONDS = 30 * 24 * 60 * 60

_KEY_INFO = b"aflow-app-server/browser-session/v1"
_ENCODED_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_COOKIE_LENGTH = 1024


class InvalidSessionError(ValueError):
    """Raised for malformed, overlong, expired, or badly signed sessions."""


def _now() -> float:
    return time.time()


def session_signing_key(auth_token: str) -> bytes:
    """Derive a browser-session key that is domain-separated from the token."""
    return hmac.new(auth_token.encode("utf-8"), _KEY_INFO, hashlib.sha256).digest()


@dataclass(frozen=True)
class BrowserSession:
    version: int
    nonce: str
    expires_at: int

    @property
    def max_age(self) -> int:
        remaining = self.expires_at - int(_now())
        return max(remaining, 0)


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(data: str) -> bytes:
    if not _ENCODED_PATTERN.fullmatch(data):
        raise InvalidSessionError("invalid session encoding")
    padding = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data + padding)
    except (ValueError, TypeError) as exc:
        raise InvalidSessionError("invalid session encoding") from exc


def encode_session(auth_token: str, *, now: float | None = None) -> str:
    """Create a signed session value expiring 30 days from ``now``."""
    issued_at = _now() if now is None else now
    payload = json.dumps(
        {
            "v": SESSION_VERSION,
            "n": secrets.token_hex(16),
            "exp": int(issued_at + SESSION_MAX_AGE_SECONDS),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    body = _b64encode(payload)
    signature = hmac.new(session_signing_key(auth_token), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64encode(signature)}"


def verify_session(value: str, auth_token: str, *, now: float | None = None) -> BrowserSession:
    """Verify a session value against the current token and return its payload.

    Rejects malformed, overlong, expired, wrong-version, and badly signed
    values without including the rejected material in the error.
    """
    if not value or len(value) > _MAX_COOKIE_LENGTH:
        raise InvalidSessionError("invalid session")
    body, separator, signature = value.partition(".")
    if not separator or not body or not signature:
        raise InvalidSessionError("invalid session")
    expected_signature = hmac.new(
        session_signing_key(auth_token), body.encode("ascii"), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(_b64decode(signature), expected_signature):
        raise InvalidSessionError("invalid session")
    try:
        payload = json.loads(_b64decode(body))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidSessionError("invalid session") from exc
    if not isinstance(payload, dict):
        raise InvalidSessionError("invalid session")
    version = payload.get("v")
    nonce = payload.get("n")
    expires_at = payload.get("exp")
    if version != SESSION_VERSION or not isinstance(nonce, str) or not nonce:
        raise InvalidSessionError("invalid session")
    if not isinstance(expires_at, int) or isinstance(expires_at, bool):
        raise InvalidSessionError("invalid session")
    verified_at = _now() if now is None else now
    if expires_at <= verified_at:
        raise InvalidSessionError("expired session")
    return BrowserSession(version=version, nonce=nonce, expires_at=expires_at)
