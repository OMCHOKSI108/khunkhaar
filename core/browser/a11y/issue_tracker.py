"""Unified Issue Intelligence: normalization, deduplication, and build-to-build regression tracking."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from core.browser.a11y.models import FindingKind, IssueCategory, IssueStatus, Severity, UXIssue


def compute_issue_fingerprint(category: str, subcategory: str, selector: str | None, title: str) -> str:
    seed = f"{category}|{subcategory}|{selector or 'none'}|{title.lower().strip()}"
    return f"ISS-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:10]}"


def calculate_severity(category: IssueCategory, finding_kind: FindingKind, blocks_journey: bool = False) -> Severity:
    if blocks_journey:
        return Severity.CRITICAL
    if category in (IssueCategory.KEYBOARD, IssueCategory.FOCUS) and finding_kind == FindingKind.INTERACTION_FAILURE:
        return Severity.HIGH
    if category == IssueCategory.ACCESSIBILITY and finding_kind == FindingKind.AUTOMATED_VIOLATION:
        return Severity.HIGH
    if category == IssueCategory.RESPONSIVE:
        return Severity.MEDIUM
    return Severity.LOW


class IssueTracker:
    def __init__(self, storage_path: Path | None = None):
        self.storage_path = storage_path
        self._issues: dict[str, UXIssue] = {}
        if storage_path and storage_path.exists():
            self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.storage_path.read_text(encoding="utf-8"))
            for item in data:
                iss = UXIssue(
                    issue_id=item["issue_id"],
                    category=IssueCategory(item["category"]),
                    subcategory=item["subcategory"],
                    title=item["title"],
                    severity=Severity(item["severity"]),
                    confidence=item["confidence"],
                    status=IssueStatus(item["status"]),
                    finding_kind=FindingKind(item["finding_kind"]),
                    url=item["url"],
                    selector=item.get("selector"),
                    reproduction=item.get("reproduction", []),
                    evidence=item.get("evidence", {}),
                    suggested_fix=item.get("suggested_fix", ""),
                    first_seen=item.get("first_seen", time.time()),
                    last_seen=item.get("last_seen", time.time()),
                )
                self._issues[iss.issue_id] = iss
        except Exception:
            pass

    def record_issue(self, issue: UXIssue) -> UXIssue:
        if issue.issue_id in self._issues:
            existing = self._issues[issue.issue_id]
            if existing.status == IssueStatus.FIXED:
                issue.status = IssueStatus.REGRESSED
            else:
                issue.status = IssueStatus.EXISTING
            issue.first_seen = existing.first_seen
            issue.last_seen = time.time()
        else:
            issue.status = IssueStatus.NEW

        self._issues[issue.issue_id] = issue
        self._save()
        return issue

    def mark_fixed_if_absent(self, current_active_ids: set[str]) -> list[str]:
        fixed = []
        for i_id, iss in self._issues.items():
            if i_id not in current_active_ids and iss.status in (IssueStatus.NEW, IssueStatus.EXISTING, IssueStatus.REGRESSED):
                iss.status = IssueStatus.FIXED
                fixed.append(i_id)
        self._save()
        return fixed

    def _save(self) -> None:
        if self.storage_path:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            data = [iss.to_dict() for iss in self._issues.values()]
            self.storage_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def get_issues(self) -> list[UXIssue]:
        return list(self._issues.values())
