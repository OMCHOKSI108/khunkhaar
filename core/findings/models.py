"""Finding model for Phase 2.

Severity (business impact) and confidence (evidence strength) are deliberately separate. A
finding also carries its reproducibility ratio, the exact reproduction steps and evidence
references so a human can re-run it without the agent.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

CATEGORIES = ("functional", "security", "ux", "accessibility", "api", "database",
              "performance", "concurrency", "business_logic", "reliability")
SEVERITIES = ("critical", "high", "medium", "low", "informational")
STATUSES = ("unverified", "likely", "confirmed", "not_reproducible", "false_positive",
            "needs_human")
# Only an independent verifier may move a finding out of "unverified".
VERIFIER_STATUSES = frozenset(STATUSES) - {"unverified"}
CONFIDENCE_BY_STATUS = {
    "confirmed": 0.95,
    "likely": 0.7,
    "unverified": 0.4,
    "not_reproducible": 0.1,
    "false_positive": 0.05,
    "needs_human": 0.3,
}
_NORMALIZE = re.compile(r"[^a-z0-9]+")


def _norm(text: str) -> str:
    return _NORMALIZE.sub(" ", (text or "").lower()).strip()


def fingerprint(project_id: str, category: str, feature: str, title: str,
                rule_id: str | None = None) -> str:
    """Stable identity so a rediscovered defect updates one row instead of flooding the store."""
    basis = "|".join([project_id, category, _norm(feature), _norm(title), rule_id or ""])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]


@dataclass
class Finding:
    project_id: str
    feature: str
    category: str
    title: str
    description: str
    severity: str
    role: str = "anonymous"
    initial_state: str = ""
    steps: list[str] = field(default_factory=list)
    expected: str = ""
    actual: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    remediation: str = ""
    source_refs: list[str] = field(default_factory=list)
    rule_ids: list[str] = field(default_factory=list)
    status: str = "unverified"
    confidence: float = 0.4
    reproducibility: str = "0/0"
    fingerprint: str = ""
    finding_id: str = ""
    invariant_id: str = ""
    reproduction: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.category not in CATEGORIES:
            raise ValueError(f"unknown category '{self.category}'")
        if self.severity not in SEVERITIES:
            raise ValueError(f"unknown severity '{self.severity}'")
        if self.status not in STATUSES:
            raise ValueError(f"unknown status '{self.status}'")
        self.fingerprint = self.fingerprint or fingerprint(
            self.project_id, self.category, self.feature, self.title,
            self.rule_ids[0] if self.rule_ids else None)
        self.finding_id = self.finding_id or f"F-{self.fingerprint[:10]}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "fingerprint": self.fingerprint,
            "project_id": self.project_id,
            "feature": self.feature,
            "category": self.category,
            "title": self.title,
            "description": self.description,
            "severity": self.severity,
            "confidence": round(self.confidence, 3),
            "reproducibility": self.reproducibility,
            "status": self.status,
            "role": self.role,
            "initial_state": self.initial_state,
            "steps": self.steps,
            "expected": self.expected,
            "actual": self.actual,
            "evidence": self.evidence,
            "remediation": self.remediation,
            "source_refs": self.source_refs,
            "rule_ids": self.rule_ids,
            "invariant_id": self.invariant_id,
            "reproduction": self.reproduction,
        }
