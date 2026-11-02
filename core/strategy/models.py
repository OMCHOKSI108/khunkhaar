"""Strategy and TestCase models for adaptive 18-category test planning."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TestCase:
    __test__ = False

    id: str
    category: str
    title: str
    priority: str  # P0_CRITICAL, P1_HIGH, P2_MEDIUM, P3_LOW
    target_type: str  # endpoint, ui_page, db_model, workflow
    target: str
    description: str
    preconditions: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    expected_behavior: str = ""
    assertion_type: str = "response_or_state"
    status: str = "pending"  # pending, passed, suspected, confirmed, skipped
    evidence_path: str = ""
    failure_details: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "title": self.title,
            "priority": self.priority,
            "target_type": self.target_type,
            "target": self.target,
            "description": self.description,
            "preconditions": self.preconditions,
            "steps": self.steps,
            "expected_behavior": self.expected_behavior,
            "assertion_type": self.assertion_type,
            "status": self.status,
            "evidence_path": self.evidence_path,
            "failure_details": self.failure_details,
        }


@dataclass
class TestStrategy:
    __test__ = False

    project_id: str
    project_name: str
    summary: str
    test_cases: list[TestCase] = field(default_factory=list)

    @property
    def total_count(self) -> int:
        return len(self.test_cases)

    @property
    def p0_count(self) -> int:
        return sum(1 for tc in self.test_cases if tc.priority == "P0_CRITICAL")

    @property
    def p1_count(self) -> int:
        return sum(1 for tc in self.test_cases if tc.priority == "P1_HIGH")

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "project_name": self.project_name,
            "summary": self.summary,
            "total_count": self.total_count,
            "p0_count": self.p0_count,
            "p1_count": self.p1_count,
            "test_cases": [tc.to_dict() for tc in self.test_cases],
        }

