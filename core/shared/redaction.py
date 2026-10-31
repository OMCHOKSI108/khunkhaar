"""Credential/secret redaction used by every MCP server and every persisted artifact.

Two guarantees the rest of the platform relies on:
  1. No secret value ever reaches model-visible tool output, a report, or the metadata store.
  2. Redaction is applied structurally (by key name) as well as textually, so a secret nested
     inside a JSON body is caught even when the header itself is absent.
"""

from __future__ import annotations

import re
from typing import Any

SECRET_KEY_PATTERNS = (
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x-amz-security-token",
    "x-goog-api-key",
    "api-key",
    "apikey",
    "password",
    "passwd",
    "secret",
    "token",
    "access_token",
    "refresh_token",
    "id_token",
    "client_secret",
    "private_key",
    "credential",
    "session",
    "csrf",
)

# Bare connection strings keep their scheme so a reader can still tell what kind of value was
# removed. Well-known key formats have no scheme and are replaced wholesale.
_URL_PATTERNS = (
    re.compile(r"(?i)\bpostgres(?:ql)?://[^\s'\"]+"),
    re.compile(r"(?i)\bmysql\+[a-z0-9_]*://[^\s'\"]+"),
    re.compile(r"(?i)\bmongodb(?:\+srv)?://[^\s'\"]+"),
    re.compile(r"(?i)\bredis[s]?://[^\s'\"]+"),
)
_TOKEN_PATTERNS = (
    re.compile(r"(?i)\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)\bxox[abpr]-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?i)\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\beyJ[A-Za-z0-9_/+\-]{20,}\.[A-Za-z0-9_/+\-]{10,}\.[A-Za-z0-9_/+\-]{10,}"),
)

REDACTED = "***REDACTED***"

_KEY_SPLIT = re.compile(r"[^a-z0-9]+")


def is_secret_key(key: str) -> bool:
    normalized = (key or "").strip().lower()
    parts = {p for p in _KEY_SPLIT.split(normalized) if p}
    if normalized.replace("-", "_") in SECRET_KEY_PATTERNS:
        return True
    for pattern in SECRET_KEY_PATTERNS:
        if pattern in parts:
            return True
        # "password_confirm", "bearer_token" style composites
        if "_" in pattern and pattern.replace("_", "") in normalized.replace("_", "").replace("-", ""):
            return True
        if len(pattern) > 5 and pattern in normalized:
            return True
    return False


def redact_text(text: str) -> str:
    if not isinstance(text, str) or not text:
        return text
    out = text
    for pattern in _URL_PATTERNS:
        out = pattern.sub(lambda m: m.group(0).split("//")[0] + "://" + REDACTED, out)
    for pattern in _TOKEN_PATTERNS:
        out = pattern.sub(REDACTED, out)
    return out


def _is_secret_bearing(value: Any) -> bool:
    """Booleans, numbers and null cannot carry a credential, so they must survive redaction.

    Without this, an API's own "has_authorization": true assertion field would be replaced
    by the redaction marker and the tool would stop being usable for verification.
    """
    return not isinstance(value, (bool, int, float)) and value is not None


def redact_obj(value: Any) -> Any:
    """Recursively redact dict/list structures by key name, plus string content patterns."""
    if isinstance(value, dict):
        return {
            k: (REDACTED if (is_secret_key(str(k)) and _is_secret_bearing(v)) else redact_obj(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_obj(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_headers(headers: dict[str, str] | Any) -> dict[str, str]:
    return {
        str(k): (REDACTED if is_secret_key(str(k)) else redact_text(str(v)))
        for k, v in dict(headers).items()
    }


def redact_query_string(query: str) -> str:
    """Redact values of secret-named query parameters without touching the rest."""
    if not query:
        return query
    out_parts = []
    for chunk in query.split("&"):
        if "=" in chunk:
            key, _, val = chunk.partition("=")
            out_parts.append(f"{key}={REDACTED}" if is_secret_key(key) else f"{key}={val}")
        else:
            out_parts.append(chunk)
    return "&".join(out_parts)
