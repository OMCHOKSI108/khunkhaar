"""Workspace manager for persistent testing artifacts and progress tracking in .khunkhar/."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.recon.models import ProjectRecon
from core.strategy.models import TestStrategy, TestCase


class WorkspaceManager:
    """Manages persistent testing state, artifacts, bugs, and reports inside .khunkhar/."""

    def __init__(self, target_root: Path | str, workspace_name: str = ".khunkhar") -> None:
        self.target_root = Path(target_root).resolve()
        self.workspace_dir = self.target_root / workspace_name
        self.bugs_dir = self.workspace_dir / "bugs"
        self.evidence_dir = self.workspace_dir / "evidence"

    def ensure_directories(self) -> None:
        """Create workspace directories if they do not exist."""
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.bugs_dir.mkdir(parents=True, exist_ok=True)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)

    def init_workspace(self, recon: ProjectRecon, strategy: TestStrategy) -> None:
        """Initialize workspace with project context, architecture, test strategy, and test cases."""
        self.ensure_directories()

        # 1. project-context.md
        context_content = (
            f"# Project Context: {recon.project_name}\n\n"
            f"- **Primary Language**: {recon.primary_language}\n"
            f"- **Project Type**: {recon.project_type}\n"
            f"- **Frameworks**: {', '.join(recon.frameworks) if recon.frameworks else 'None detected'}\n"
            f"- **Suggested Target URL**: {recon.suggested_target_url or 'N/A'}\n"
            f"- **Discovered Ports**: {', '.join(f'{p.port} ({p.service_type})' for p in recon.ports)}\n"
            f"- **Auth Mechanisms**: {', '.join(recon.auth_mechanisms) if recon.auth_mechanisms else 'None specified'}\n\n"
            f"## Discovered Endpoints ({len(recon.endpoints)})\n"
            f"- GraphQL Queries: {sum(1 for e in recon.endpoints if e.kind == 'graphql_query')}\n"
            f"- GraphQL Mutations: {sum(1 for e in recon.endpoints if e.kind == 'graphql_mutation')}\n"
            f"- REST / UI Endpoints: {sum(1 for e in recon.endpoints if e.kind in ('rest', 'page'))}\n\n"
            f"## Database Models ({len(recon.models)})\n"
            + "\n".join(f"- `{m.name}` ({m.table_name})" for m in recon.models[:30])
        )
        (self.workspace_dir / "project-context.md").write_text(context_content, encoding="utf-8")

        # 2. test-cases.json
        (self.workspace_dir / "test-cases.json").write_text(
            json.dumps(strategy.to_dict(), indent=2), encoding="utf-8"
        )

        # 3. execution-log.json (if not existing)
        log_file = self.workspace_dir / "execution-log.json"
        if not log_file.is_file():
            initial_log = {
                "started_at": datetime.now(timezone.utc).isoformat(),
                "completed_tests": {},
                "total_tests": strategy.total_count,
            }
            log_file.write_text(json.dumps(initial_log, indent=2), encoding="utf-8")

    def record_execution(self, test_id: str, status: str, duration_sec: float, details: str = "") -> None:
        """Record the outcome of a test case in execution-log.json."""
        log_file = self.workspace_dir / "execution-log.json"
        log_data = {"completed_tests": {}}
        if log_file.is_file():
            try:
                log_data = json.loads(log_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        log_data.setdefault("completed_tests", {})[test_id] = {
            "status": status,
            "duration_sec": duration_sec,
            "details": details,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        log_file.write_text(json.dumps(log_data, indent=2), encoding="utf-8")

    def record_bug(
        self,
        test_id: str,
        title: str,
        severity: str,
        description: str,
        steps_to_reproduce: list[str],
        expected_behavior: str,
        actual_behavior: str,
        evidence: str = "",
        payload: str = "",
    ) -> Path:
        """Create a structured markdown bug artifact in bugs/."""
        self.ensure_directories()
        clean_name = re.sub(r'[^a-zA-Z0-9_-]', '_', f"{test_id}_{title}")[:60].lower()
        bug_path = self.bugs_dir / f"{clean_name}.md"
        bug_content = (
            f"# Bug Report: {title}\n\n"
            f"- **Test Case ID**: `{test_id}`\n"
            f"- **Severity**: **{severity}**\n"
            f"- **Detected**: {datetime.now(timezone.utc).isoformat()}\n\n"
            f"## Description\n{description}\n\n"
            f"## Steps to Reproduce\n"
            + "\n".join(f"{i+1}. {step}" for i, step in enumerate(steps_to_reproduce))
            + f"\n\n## Expected Behavior\n{expected_behavior}\n\n"
            f"## Actual Behavior\n{actual_behavior}\n\n"
            f"## Evidence & Payload\n"
            f"```\n{payload or 'No raw payload'}\n```\n\n"
            f"```json\n{evidence or 'No JSON evidence'}\n```\n"
        )
        bug_path.write_text(bug_content, encoding="utf-8")
        return bug_path

    def generate_final_report(self, recon: ProjectRecon, strategy: TestStrategy) -> Path:
        """Compile a comprehensive Senior QA Engineering audit report."""
        report_path = self.workspace_dir / "final-report.md"
        bugs = list(self.bugs_dir.glob("*.md"))
        log_file = self.workspace_dir / "execution-log.json"
        completed = {}
        if log_file.is_file():
            try:
                completed = json.loads(log_file.read_text(encoding="utf-8")).get("completed_tests", {})
            except Exception:
                pass

        report_lines = [
            f"# Senior QA & Security Audit Report: {recon.project_name}",
            f"\n**Audit Date**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            f"**Target Architecture**: {recon.primary_language} | {recon.project_type} | {', '.join(recon.frameworks)}",
            f"**Target URL**: {recon.suggested_target_url or 'N/A'}",
            "\n## Executive Summary",
            f"- **Total Strategy Test Cases**: {strategy.total_count}",
            f"- **Executed Tests**: {len(completed)}",
            f"- **Confirmed Vulnerabilities / Bugs**: {len(bugs)}",
            "\n## Confirmed Issues & Findings",
        ]

        if not bugs:
            report_lines.append("\n*No defects or security issues identified during the test run.*")
        else:
            for b in sorted(bugs):
                report_lines.append(f"\n### {b.stem.replace('_', ' ').title()}\n")
                report_lines.append(b.read_text(encoding="utf-8"))

        report_lines.extend([
            "\n## Senior QA Recommendations",
            "1. **Enforce Parameterized Queries**: Ensure every database query uses ORM parameter binding or parameterized SQL fragments.",
            "2. **Strict RBAC & Tenant Scoping**: Enforce ownership checks on all single-entity lookups (`where: { id }`).",
            "3. **Side-Effect Verification**: Ensure notification queues and audit trails are atomically written within database transactions.",
        ])

        report_path.write_text("\n".join(report_lines), encoding="utf-8")
        return report_path



