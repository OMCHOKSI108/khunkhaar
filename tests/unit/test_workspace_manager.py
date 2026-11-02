"""Tests for WorkspaceManager and persistent QA artifacts."""

from pathlib import Path
import json
import pytest

from core.recon.models import ProjectRecon, EndpointDef
from core.strategy.models import TestStrategy, TestCase
from core.workspace.manager import WorkspaceManager


def test_workspace_manager_lifecycle(tmp_path: Path):
    ws = WorkspaceManager(tmp_path, workspace_name=".test_khunkhar")
    recon = ProjectRecon(
        project_id="p99",
        project_name="SampleApp",
        root_path=str(tmp_path),
        primary_language="Python",
        frameworks=["FastAPI"],
        endpoints=[EndpointDef(name="testEp", kind="rest", path="/test")],
    )
    strategy = TestStrategy(
        project_id="p99",
        project_name="SampleApp",
        summary="Test summary",
        test_cases=[TestCase(id="TC-01", category="SQLi", title="Test 1", priority="P0_CRITICAL", target_type="endpoint", target="testEp", description="Test")],
    )

    ws.init_workspace(recon, strategy)

    assert (ws.workspace_dir / "project-context.md").is_file()
    assert (ws.workspace_dir / "test-cases.json").is_file()
    assert (ws.workspace_dir / "execution-log.json").is_file()

    # Record execution and bug
    ws.record_execution("TC-01", "failed", 1.25, "Injected SQL succeeded")
    bug_path = ws.record_bug(
        test_id="TC-01",
        title="SQL Injection on testEp",
        severity="Critical",
        description="Search input unescaped",
        steps_to_reproduce=["Send payload ' OR 1=1"],
        expected_behavior="400 Bad Request",
        actual_behavior="Leaked 50 records",
    )
    assert bug_path.is_file()

    # Generate final report
    report_path = ws.generate_final_report(recon, strategy)
    assert report_path.is_file()
    report_text = report_path.read_text(encoding="utf-8")
    assert "SampleApp" in report_text
    assert "Critical" in report_text
