"""Sandbox Agent Tools: agent-callable functions to execute untrusted code safely."""
from __future__ import annotations

import uuid
from typing import Any

from core.sandbox.filesystem import BASE_SANDBOX_DIR
from core.sandbox.models import NetworkPolicy, ResourceLimits, SandboxJob
from core.sandbox.runner import execute_in_sandbox, terminate_and_cleanup
from core.sandbox.store_db import get_execution_artifacts as _db_get_artifacts, get_execution_record


def run_in_sandbox(
    script: str,
    language: str = "python",
    execution_profile: str = "python_qa",
    network_policy: str = "none",
    target_url: str | None = None,
    target_id: str | None = None,
    timeout_seconds: int = 120,
    collect_artifacts: bool = True,
    memory_mb: int = 512,
) -> dict[str, Any]:
    """Execute code in an isolated, ephemeral sandbox environment."""
    try:
        policy = NetworkPolicy(network_policy.lower())
    except ValueError:
        policy = NetworkPolicy.NONE

    limits = ResourceLimits(
        memory_mb=min(memory_mb, 1024),
        max_memory_mb=min(int(memory_mb * 1.5), 1536),
        timeout_seconds=min(timeout_seconds, 300),
    )

    job_id = f"sbx-{uuid.uuid4().hex[:12]}"
    job = SandboxJob(
        id=job_id,
        language=language,
        execution_profile=execution_profile,
        script=script,
        network_policy=policy,
        target_id=target_id,
        target_url=target_url,
        timeout_seconds=limits.timeout_seconds,
        collect_artifacts=collect_artifacts,
        limits=limits,
    )

    result = execute_in_sandbox(job)
    return result.to_dict()


def get_execution_status(execution_id: str) -> dict[str, Any]:
    """Check current status and execution metrics for a sandbox run."""
    record = get_execution_record(execution_id)
    if not record:
        return {"execution_id": execution_id, "status": "NOT_FOUND"}
    return record


def get_execution_logs(execution_id: str) -> dict[str, Any]:
    """Retrieve captured stdout and stderr streams from a sandbox execution."""
    logs_dir = BASE_SANDBOX_DIR / execution_id / "logs"
    stdout_file = logs_dir / "stdout.log"
    stderr_file = logs_dir / "stderr.log"

    stdout = stdout_file.read_text(encoding="utf-8", errors="replace") if stdout_file.exists() else ""
    stderr = stderr_file.read_text(encoding="utf-8", errors="replace") if stderr_file.exists() else ""

    return {
        "execution_id": execution_id,
        "stdout": stdout,
        "stderr": stderr,
    }


def get_execution_artifacts(execution_id: str) -> list[dict[str, Any]]:
    """List quarantined artifacts produced during a sandbox execution."""
    return _db_get_artifacts(execution_id)


def terminate_execution(execution_id: str) -> dict[str, Any]:
    """Terminate sandbox job and purge ephemeral workspace."""
    success = terminate_and_cleanup(execution_id)
    return {"execution_id": execution_id, "terminated": success}
