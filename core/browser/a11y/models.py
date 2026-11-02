"""Data models and schemas for Accessibility & UX Intelligence Engine v2."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class IssueCategory(str, Enum):
    ACCESSIBILITY = "accessibility"
    KEYBOARD = "keyboard"
    FOCUS = "focus"
    FORMS = "forms"
    RESPONSIVE = "responsive"
    VISUAL = "visual"
    JOURNEY = "journey"


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class IssueStatus(str, Enum):
    NEW = "new"
    EXISTING = "existing"
    FIXED = "fixed"
    REGRESSED = "regressed"
    INCONCLUSIVE = "inconclusive"


class FindingKind(str, Enum):
    AUTOMATED_VIOLATION = "automated_violation"
    INTERACTION_FAILURE = "interaction_failure"
    MANUAL_REVIEW_REQUIRED = "manual_review_required"
    POTENTIAL_ISSUE = "potential_issue"


@dataclass
class UXIssue:
    issue_id: str
    category: IssueCategory
    subcategory: str
    title: str
    severity: Severity
    confidence: float
    status: IssueStatus
    url: str
    selector: str | None = None
    wcag_criterion: str | None = None
    wcag_level: str | None = None
    finding_kind: FindingKind = FindingKind.AUTOMATED_VIOLATION
    reproduction: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    suggested_fix: str = ""
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_id": self.issue_id,
            "category": self.category.value,
            "subcategory": self.subcategory,
            "title": self.title,
            "severity": self.severity.value,
            "confidence": round(self.confidence, 2),
            "status": self.status.value,
            "finding_kind": self.finding_kind.value,
            "url": self.url,
            "selector": self.selector,
            "wcag": {"criterion": self.wcag_criterion, "level": self.wcag_level} if self.wcag_criterion else None,
            "reproduction": self.reproduction,
            "evidence": self.evidence,
            "suggested_fix": self.suggested_fix,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
        }


@dataclass
class JourneyStep:
    action: str  # navigate, click, fill, select, press, wait
    target: str | dict[str, str]  # css selector or {"role": ..., "name": ...}
    value: str | None = None
    assertion: str | None = None  # e.g., "url_contains:...", "text_present:...", "visible:..."


@dataclass
class JourneyResult:
    journey_name: str
    success: bool
    steps_total: int
    steps_completed: int
    failed_step_index: int | None = None
    error_message: str | None = None
    step_records: list[dict[str, Any]] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
