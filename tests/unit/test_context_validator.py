"""Unit tests for the 5-level TCB Validation Engine."""
from pathlib import Path

from core.context_builder import tools
from core.context_validator import (
    agent_comparator,
    benchmark_repo,
    freshness_validator,
    retrieval_validator,
    runner,
    structural_validator,
)


def test_benchmark_repo_generation(tmp_path: Path):
    cases = benchmark_repo.populate_benchmark_repo(tmp_path)
    assert len(cases) >= 4
    assert (tmp_path / "src" / "controllers" / "attendance.controller.ts").exists()
    assert (tmp_path / "src" / "services" / "attendance.service.ts").exists()


def test_level_1_structural_validation(tmp_path: Path):
    benchmark_repo.populate_benchmark_repo(tmp_path)
    report = structural_validator.validate_ingestion_and_indexing(tmp_path)
    assert report.passed is True
    assert report.score_percentage >= 98.0
    assert any(m.name == "file_discovery_accuracy" and m.value >= 0.99 for m in report.metrics)


def test_level_2_and_3_retrieval(tmp_path: Path):
    cases = benchmark_repo.populate_benchmark_repo(tmp_path)
    tools.build_full_project_context("test_retrieval_proj", str(tmp_path))
    report = retrieval_validator.validate_retrieval_and_understanding("test_retrieval_proj", cases)
    assert report.score_percentage >= 70.0
    assert any(m.name == "recall_at_5" for m in report.metrics)


def test_level_4_freshness_updates(tmp_path: Path):
    benchmark_repo.populate_benchmark_repo(tmp_path)
    report = freshness_validator.validate_freshness_and_updates("test_fresh_proj", tmp_path)
    assert report.passed is True
    assert any(m.name == "stale_record_purging" and m.value == 1.0 for m in report.metrics)


def test_level_5_agent_comparison(tmp_path: Path):
    benchmark_repo.populate_benchmark_repo(tmp_path)
    tools.build_full_project_context("test_agent_proj", str(tmp_path))
    report = agent_comparator.validate_agent_performance("test_agent_proj", "punch attendance service")
    assert report.passed is True
    assert any(m.name == "token_budget_compliance" and m.passed for m in report.metrics)


def test_full_comprehensive_validation_runner():
    summary = runner.execute_comprehensive_validation("test_master_proj")
    assert "overall_score" in summary
    assert "level_reports" in summary
    assert len(summary["level_reports"]) == 4
    assert summary["overall_score"] >= 80.0
