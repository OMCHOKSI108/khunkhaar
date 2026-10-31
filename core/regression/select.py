"""Choose the findings that may become regression tests.

Only a finding the independent verifier marked `confirmed` is eligible. Everything else is
returned as excluded with the reason, because silently dropping half the store would make the
generation run look more complete than it is.
"""

from __future__ import annotations

from typing import Any

from core.findings import store as findings_store
from core.shared.config import current

EVIDENCE_STATUSES = ("confirmed", "likely")


def _has_scenario(finding: dict[str, Any]) -> bool:
    reproduction = finding.get("reproduction") or {}
    if reproduction.get("type") == "journey":
        return True
    return bool(reproduction.get("probe_kind") or (reproduction.get("invariant") or {}).get("kind"))


def select(project_id: str, *, findings: list[dict[str, Any]] | None = None,
           require_confirmed: bool | None = None) -> dict[str, Any]:
    if findings is None:
        findings = findings_store.list_findings(project_id)
    if require_confirmed is None:
        require_confirmed = bool(current().get("regression", "require_confirmed_finding", True))
    eligible: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for finding in findings:
        status = str(finding.get("status") or "")
        allowed = ("confirmed",) if require_confirmed else EVIDENCE_STATUSES
        if status not in allowed:
            excluded.append({
                "finding_id": finding.get("id"), "status": status,
                "title": finding.get("title"),
                "reason": (f"status is {status}; only an independently confirmed finding may "
                           f"generate a regression test"),
            })
            continue
        if not _has_scenario(finding):
            excluded.append({
                "finding_id": finding.get("id"), "status": status,
                "title": finding.get("title"),
                "reason": ("no machine-reproducible scenario was recorded; this finding needs "
                           "human judgment before a test can be generated"),
            })
            continue
        eligible.append(finding)
    return {"eligible": eligible, "excluded": excluded}
