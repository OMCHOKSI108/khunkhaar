"""Data models for TCB Validation Engine."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class BenchmarkCase:
    query: str
    expected_files: list[str]
    expected_symbols: list[str] = field(default_factory=list)
    expected_flow: list[str] = field(default_factory=list)
    description: str = ""


@dataclass
class MetricResult:
    name: str
    value: float
    target: float
    passed: bool
    details: str = ""


@dataclass
class LevelReport:
    level: int
    name: str
    passed: bool
    score_percentage: float
    metrics: list[MetricResult] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


@dataclass
class ValidationReport:
    project_id: str
    timestamp: str
    overall_passed: bool
    overall_score: float
    level_reports: list[LevelReport] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "timestamp": self.timestamp,
            "overall_passed": self.overall_passed,
            "overall_score": self.overall_score,
            "level_reports": [
                {
                    "level": lr.level,
                    "name": lr.name,
                    "passed": lr.passed,
                    "score": lr.score_percentage,
                    "metrics": [
                        {"name": m.name, "value": m.value, "target": m.target, "passed": m.passed, "details": m.details}
                        for m in lr.metrics
                    ],
                    "failures": lr.failures,
                }
                for lr in self.level_reports
            ],
            "recommendations": self.recommendations,
        }
