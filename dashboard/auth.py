"""Token authentication for the loopback dashboard.

The token lives in `data/profiles/dashboard/token`, mode 0600, and is generated on first start.
The browser never stores the token itself: a login derives a short-lived cookie value with an
HMAC keyed by the token, and every comparison goes through `hmac.compare_digest`.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path

from core.shared import paths

COOKIE_NAME = "khunkhaar_dashboard"
SESSION_TTL_SECONDS = 8 * 60 * 60
_TOKEN_CONTEXT = b"khunkhaar-dashboard-session-v1"
_MIN_TOKEN_CHARS = 32


class DashboardAuthError(RuntimeError):
    """The token file cannot be used safely; the dashboard refuses to start."""


def token_path() -> Path:
    return paths.dashboard_secrets_dir() / "token"


def ensure_token() -> str:
    """Return the token, creating it with mode 0600 on first use.

    An existing file is tightened to 0600 rather than trusted; a symlink or a too-short value is
    refused instead of silently regenerating what an attacker may have swapped.
    """
    directory = token_path().parent
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass  # a mounted filesystem may not support chmod; the file mode below still applies
    path = token_path()
    if path.is_symlink():
        raise DashboardAuthError(f"the dashboard token path is a symlink: {path}")
    if path.exists():
        path.chmod(0o600)
        token = path.read_text(encoding="utf-8").strip()
        if len(token) < _MIN_TOKEN_CHARS:
            raise DashboardAuthError(
                f"the dashboard token in {path} is too short; remove it to generate a new one")
        return token
    token = secrets.token_urlsafe(32)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:  # two starts raced; the winner's token is the one that counts
        return ensure_token()
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    return token


def tokens_match(provided: str | None, expected: str | None) -> bool:
    """Constant-time comparison for tokens and derived session values."""
    return hmac.compare_digest((provided or "").encode("utf-8"),
                               (expected or "").encode("utf-8"))


def session_value(token: str, issued_at: int | None = None) -> str:
    moment = int(time.time()) if issued_at is None else int(issued_at)
    message = f"{_TOKEN_CONTEXT.decode()}:{moment}".encode("utf-8")
    mac = hmac.new(token.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"{moment}.{mac}"


def valid_session(cookie: str | None, token: str, *, now: int | None = None) -> bool:
    if not cookie or "." not in cookie:
        return False
    raw, _, _ = cookie.partition(".")
    try:
        issued_at = int(raw)
    except ValueError:
        return False
    if not tokens_match(cookie, session_value(token, issued_at)):
        return False
    age = (int(time.time()) if now is None else int(now)) - issued_at
    return 0 <= age <= SESSION_TTL_SECONDS
