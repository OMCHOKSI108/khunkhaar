"""Outbound-target preflight shared by every capability that is about to send requests.

A repository registration is never permission to test. A target may receive requests only when
it passes the outbound policy (scheme, allowlist, address class) and, for a non-loopback host,
has an unexpired authorization grant recorded for this project.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from core.metadata import store
from core.shared import netguard
from core.shared.config import current

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def is_loopback(target: str | None) -> bool:
    if not target:
        return True
    return netguard.normalize_host(urlsplit(target).hostname or "") in LOOPBACK


def preflight_target(project_id: str, target: str) -> str | None:
    """None when the target may receive requests, otherwise the refusal to print."""
    parts = urlsplit(target)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "the target must be an http(s) URL with a host"
    decision = netguard.check_target(target, current())
    if not decision.allowed:
        return (f"the target is outside the outbound policy: {decision.reason}. Add the host to "
                f"api.allowed_hosts if it is a disposable test environment.")
    if is_loopback(target):
        return None
    granted = [item for item in store.active_authorizations(project_id)
               if item.get("target", "").rstrip("/") == target.rstrip("/")
               and item.get("mode") in ("observe", "controlled")]
    if not granted:
        return (f"{parts.hostname} is not a loopback target and no unexpired authorization grant "
                f"covers {target} for this project. Run `khunkhaar project authorize` first - "
                f"registration alone is never testing permission.")
    return None
