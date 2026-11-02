"""Data models and enums for KHUNKHAAR-QA Ephemeral Execution Sandbox."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class NetworkPolicy(str, Enum):
    NONE = "none"
    MOCK_ONLY = "mock_only"
    ALLOWLISTED = "allowlisted"
    CONTROLLED_INTERNET = "controlled_internet"


class JobStatus(str, Enum):
    QUEUED = "QUEUED"
    VALIDATING = "VALIDATING"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    COLLECTING = "COLLECTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    TERMINATED = "TERMINATED"
    CLEANED = "CLEANED"


@dataclass
class ResourceLimits:
    cpu_cores: float = 1.0
    memory_mb: int = 512
    max_memory_mb: int = 768
    timeout_seconds: int = 120
    max_output_bytes: int = 5 * 1024 * 1024  # 5 MB
    max_processes: int = 64
    max_workspace_mb: int = 256


@dataclass
class SandboxJob:
    id: str
    language: str  # python, javascript, typescript
    execution_profile: str  # python_qa, node_qa, playwright_qa
    script: str
    network_policy: NetworkPolicy = NetworkPolicy.NONE
    target_id: str | None = None
    target_url: str | None = None
    timeout_seconds: int = 120
    collect_artifacts: bool = True
    status: JobStatus = JobStatus.QUEUED
    created_at: str = ""
    limits: ResourceLimits = field(default_factory=ResourceLimits)


@dataclass
class ResourceUsage:
    cpu_seconds: float = 0.0
    peak_memory_mb: float = 0.0
    execution_time_ms: int = 0


@dataclass
class SandboxResult:
    execution_id: str
    status: JobStatus
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    execution_time_ms: int = 0
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    resource_usage: ResourceUsage = field(default_factory=ResourceUsage)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "status": self.status.value if isinstance(self.status, JobStatus) else str(self.status),
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "execution_time_ms": self.execution_time_ms,
            "artifacts": self.artifacts,
            "resource_usage": {
                "cpu_seconds": self.resource_usage.cpu_seconds,
                "peak_memory_mb": self.resource_usage.peak_memory_mb,
                "execution_time_ms": self.resource_usage.execution_time_ms,
            },
            "error": self.error,
        }
