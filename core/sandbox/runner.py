"""Sandbox Runner: high-level coordinator of validation, execution, and artifact quarantine."""
from __future__ import annotations

import shutil
import uuid
from typing import Any

from core.sandbox.artifact_collector import collect_execution_artifacts
from core.sandbox.execution_broker import execute_script
from core.sandbox.filesystem import cleanup_execution_filesystem, prepare_execution_filesystem
from core.sandbox.models import JobStatus, ResourceUsage, SandboxJob, SandboxResult
from core.sandbox.policy import PolicyViolation, validate_sandbox_job
from core.sandbox.store_db import record_audit_log, record_execution_finish, record_execution_start


def _resolve_script_filename(language: str) -> str:
    lang = language.lower()
    if lang in ("python", "python3", "py"):
        return "script.py"
    if lang in ("javascript", "js", "node"):
        return "script.js"
    if lang in ("typescript", "ts"):
        return "script.ts"
    return "script.txt"


def execute_in_sandbox(job: SandboxJob, auto_cleanup_workspace: bool = True) -> SandboxResult:
    if not job.id:
        job.id = f"sbx-{uuid.uuid4().hex[:12]}"

    # 1. Security policy validation
    try:
        validate_sandbox_job(job)
    except PolicyViolation as pv:
        record_audit_log(job.id, "POLICY_REJECTED", "policy_engine", "DENIED", {"reason": str(pv)})
        return SandboxResult(
            execution_id=job.id,
            status=JobStatus.FAILED,
            exit_code=1,
            stderr=f"Policy Violation: {pv}",
            error=str(pv),
        )

    # 2. Ephemeral filesystem preparation
    filename = _resolve_script_filename(job.language)
    fs_info = prepare_execution_filesystem(job.id, job.script, filename)

    # 3. Record job start in SQLite
    record_execution_start(job)

    # 4. Execute script under broker supervision
    try:
        exit_code, stdout, stderr, usage, status = execute_script(job, fs_info)
    except Exception as exc:
        exit_code = 1
        stdout = ""
        stderr = f"Execution broker error: {exc}"
        usage = ResourceUsage()
        status = JobStatus.FAILED

    # 5. Collect quarantined artifacts
    artifacts: list[dict[str, Any]] = []
    if job.collect_artifacts and fs_info["artifacts"].exists():
        artifacts = collect_execution_artifacts(fs_info["artifacts"])

    result = SandboxResult(
        execution_id=job.id,
        status=status,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        execution_time_ms=usage.execution_time_ms,
        artifacts=artifacts,
        resource_usage=usage,
    )

    # 6. Record completion & artifacts
    record_execution_finish(result)

    # 7. Ephemeral workspace scrubbing (preserves logs/artifacts until explicit cleanup)
    if auto_cleanup_workspace:
        shutil.rmtree(fs_info["workspace"], ignore_errors=True)
        shutil.rmtree(fs_info["input"], ignore_errors=True)

    return result


def terminate_and_cleanup(execution_id: str) -> bool:
    record_audit_log(execution_id, "JOB_TERMINATED", "agent", "CLEANED", {})
    return cleanup_execution_filesystem(execution_id)
