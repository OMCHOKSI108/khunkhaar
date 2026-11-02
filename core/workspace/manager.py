"""Workspace manager for persistent testing artifacts and progress tracking in .khunkhar/."""

from __future__ import annotations

import json
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

