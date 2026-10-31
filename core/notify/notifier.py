"""Alert delivery with a fixed, redacted payload.

Every notification is built from named fields, not by serialising a finding, request or log.
The allowlist is: finding id, severity, feature, a one-line title, a relative evidence path and
counts. Source code, request bodies, headers, credentials, target URLs and database values never
enter a payload or a desktop message.

Terminal output always happens unless disabled. Desktop delivery uses `notify-send` when it is
installed; an absent binary or a refusal is recorded as a delivery warning and never crashes the
monitoring run. Email and webhook are deliberately not implemented: if an operator enables one,
the alert records that nothing was sent instead of silently making an outbound request.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from typing import Any

from core.metadata import store
from core.shared.config import current
from core.shared.redaction import redact_text

KINDS = {
    "smoke_failed",
    "regression_failed",
    "invariant_failed",
    "invariant_uncovered",
    "environment_unavailable",
    "monitoring_missed",
    "monitoring_blocked",
}
SEVERITIES = ("critical", "high", "medium", "low", "info")
_DETAIL_KEYS = {"schedule_id", "check", "test_id", "finding_id", "feature", "title", "severity",
                "count", "http_status", "note", "evidence", "result", "due_at"}
_URGENCY = {"critical": "critical", "high": "critical", "medium": "normal", "low": "low",
            "info": "low"}


class AlertError(ValueError):
    """An alert built with a kind or severity outside the allowlist."""


def _one_line(value: Any, limit: int = 300) -> str:
    text = " ".join(str(value or "").split())
    return redact_text(text)[:limit]


def _detail(values: dict[str, Any] | None) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in (values or {}).items():
        if key not in _DETAIL_KEYS:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            clean[key] = _one_line(value, 500) if isinstance(value, str) else value
    return clean


def notify(project_id: str, kind: str, severity: str, message: str, *,
           finding_id: str | None = None, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    """Record one alert and deliver it to the enabled local channels."""
    if kind not in KINDS:
        raise AlertError(f"unknown alert kind '{kind}'")
    if severity not in SEVERITIES:
        raise AlertError(f"unknown alert severity '{severity}'")
    fields = _detail({**(detail or {}), "finding_id": finding_id or (detail or {}).get("finding_id"),
                      "severity": severity})
    text = _one_line(message, 500)
    cfg = current().section("notifications") or {}
    deliveries: list[dict[str, Any]] = []
    channels: list[str] = []

    try:
        row = store.record_alert({
            "project_id": project_id,
            "kind": kind,
            "severity": severity,
            "finding_id": fields.get("finding_id"),
            "message": text,
            "channel": "pending",
            "status": "recorded",
            "detail": {"kind": kind, "severity": severity, **fields},
        })
    except Exception as exc:  # a notification failure must not abort the monitored check
        return {"status": "record_failed", "reason": type(exc).__name__, "deliveries": []}

    if bool(cfg.get("terminal", True)):
        channels.append("terminal")
        try:
            payload = {"kind": kind, "severity": severity, **_public(fields), "message": text}
            print("KHUNKHAAR ALERT " + json.dumps(payload, ensure_ascii=False, sort_keys=True),
                  file=sys.stderr)
            deliveries.append({"channel": "terminal", "status": "delivered"})
        except OSError as exc:
            deliveries.append({"channel": "terminal", "status": "failed",
                               "reason": type(exc).__name__})
    else:
        deliveries.append({"channel": "terminal", "status": "disabled"})

    desktop = cfg.get("desktop") or {}
    if isinstance(desktop, dict) and desktop.get("enabled"):
        channels.append("desktop")
        deliveries.append(_desktop(kind, severity, text, desktop))
    else:
        deliveries.append({"channel": "desktop", "status": "disabled"})

    for remote in ("email", "webhook"):
        section = cfg.get(remote) or {}
        if isinstance(section, dict) and section.get("enabled"):
            channels.append(remote)
            deliveries.append({
                "channel": remote, "status": "not_sent",
                "reason": "external notification channels are not implemented in Phase 3",
            })

    delivered = any(item["status"] == "delivered" for item in deliveries)
    status = "delivered" if delivered else "recorded"
    updated = store.update_alert(int(row["id"]), channel=",".join(channels) or "record",
                                 status=status, detail={"kind": kind, "severity": severity,
                                                        **fields, "deliveries": deliveries},
                                 delivered_at=row["created_at"] if delivered else None)
    return updated or {**row, "deliveries": deliveries}


def _public(fields: dict[str, Any]) -> dict[str, Any]:
    """The subset permitted in terminal and desktop text; schedule ids stay in the store."""
    return {key: value for key, value in fields.items()
            if key in {"finding_id", "feature", "title", "evidence"}}


def _desktop(kind: str, severity: str, message: str, cfg: dict[str, Any]) -> dict[str, Any]:
    binary = shutil.which("notify-send")
    if not binary:
        return {"channel": "desktop", "status": "unavailable",
                "reason": "notify-send is not installed; the alert was recorded locally"}
    timeout = max(1, int(cfg.get("timeout_seconds", 5)))
    argv = [binary, "--app-name=KHUNKHAAR QA", f"--urgency={_URGENCY[severity]}",
            f"KHUNKHAAR QA: {severity} {kind}", message]
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                                   check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"channel": "desktop", "status": "failed", "reason": type(exc).__name__}
    if completed.returncode != 0:
        return {"channel": "desktop", "status": "failed",
                "reason": f"notify-send exited {completed.returncode}"}
    return {"channel": "desktop", "status": "delivered"}
