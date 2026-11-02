"""TCB Master Validation Engine: orchestrates 5-level verification suite."""
from __future__ import annotations

import datetime
import tempfile
from pathlib import Path
from typing import Any

from core.context_builder import tools
from core.context_validator import (
    agent_comparator,
    benchmark_repo,
    freshness_validator,
    retrieval_validator,
    structural_validator,
)
from core.context_validator.models import ValidationReport


def execute_comprehensive_validation(project_id: str = "tcb_bench_suite") -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir)
        cases = benchmark_repo.populate_benchmark_repo(repo_path)

        # Build initial context
        tools.build_full_project_context(project_id, str(repo_path))

        # 1. Level 1: Ingestion & Indexing
        l1 = structural_validator.validate_ingestion_and_indexing(repo_path)

        # 2. Level 2 & 3: Retrieval & Codebase Flow Understanding
        l2 = retrieval_validator.validate_retrieval_and_understanding(project_id, cases)

        # 3. Level 4: Incremental Updates & Freshness
        l4 = freshness_validator.validate_freshness_and_updates(project_id, repo_path)

        # 4. Level 5: Agent Performance & A/B/C Comparison
        sample_task = "Investigate attendance punch recording, calculate late arrival and database model"
        l5 = agent_comparator.validate_agent_performance(project_id, sample_task)

        level_reports = [l1, l2, l4, l5]
        overall_passed = all(lr.passed for lr in level_reports)
        avg_score = round(sum(lr.score_percentage for lr in level_reports) / len(level_reports), 2)

        recommendations: list[str] = []
        for lr in level_reports:
            for fail in lr.failures:
                recommendations.append(f"[{lr.name}] {fail}")
        if not recommendations:
            recommendations.append("All 5 validation levels passed acceptance criteria. TCB is production ready.")

        report = ValidationReport(
            project_id=project_id,
            timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            overall_passed=overall_passed,
            overall_score=avg_score,
            level_reports=level_reports,
            recommendations=recommendations,
        )
        return report.to_dict()
