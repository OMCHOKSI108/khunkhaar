"""Execution Broker: manages subprocess launch, resource monitoring, and output capture."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from core.sandbox.models import JobStatus, ResourceLimits, ResourceUsage, SandboxJob
from core.sandbox.network import get_sandbox_network_env
from core.sandbox.resource_limits import cap_output_string, configure_rlimits, kill_process_tree


def resolve_executable(language: str) -> list[str]:
    lang = language.lower()
    if lang in ("python", "python3", "py"):
        return [sys.executable]
    if lang in ("javascript", "js", "node"):
        node_bin = shutil.which("node") or "/usr/bin/node"
        if not Path(node_bin).exists():
            raise RuntimeError(f"Node.js binary not found for language '{language}'")
        return [node_bin]
    if lang in ("typescript", "ts"):
        node_bin = shutil.which("node") or "/usr/bin/node"
        ts_node = shutil.which("ts-node")
        if ts_node:
            return [ts_node]
        if Path(node_bin).exists():
            return [node_bin]
        raise RuntimeError(f"TypeScript/Node runtime not found for language '{language}'")
    raise ValueError(f"Unsupported sandbox language: '{language}'")


def execute_script(
    job: SandboxJob,
    fs_info: dict[str, Path],
) -> tuple[int, str, str, ResourceUsage, JobStatus]:
    cmd_prefix = resolve_executable(job.language)
    cmd = cmd_prefix + [str(fs_info["script"])]

    env = get_sandbox_network_env(job.network_policy)
    env["KHUNKHAAR_EXECUTION_ID"] = job.id
    env["KHUNKHAAR_WORKSPACE_DIR"] = str(fs_info["workspace"])
    env["KHUNKHAAR_OUTPUT_DIR"] = str(fs_info["output"])
    env["KHUNKHAAR_ARTIFACTS_DIR"] = str(fs_info["artifacts"])
    env["KHUNKHAAR_LOGS_DIR"] = str(fs_info["logs"])

    start_time = time.perf_counter()
    status = JobStatus.RUNNING
    stdout_str = ""
    stderr_str = ""
    exit_code = -1

    proc = subprocess.Popen(
        cmd,
        cwd=str(fs_info["workspace"]),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=configure_rlimits(job.limits),
        text=True,
    )

    try:
        raw_out, raw_err = proc.communicate(timeout=job.timeout_seconds)
        exit_code = proc.returncode
        status = JobStatus.COMPLETED if exit_code == 0 else JobStatus.FAILED
        stdout_str = cap_output_string(raw_out or "", job.limits.max_output_bytes)
        stderr_str = cap_output_string(raw_err or "", job.limits.max_output_bytes)
    except subprocess.TimeoutExpired:
        kill_process_tree(proc.pid)
        try:
            raw_out, raw_err = proc.communicate(timeout=2)
            stdout_str = cap_output_string(raw_out or "", job.limits.max_output_bytes)
            stderr_str = cap_output_string(raw_err or "", job.limits.max_output_bytes)
        except Exception:
            pass
        status = JobStatus.TIMED_OUT
        stderr_str += f"\n[SANDBOX TIMEOUT: Exceeded {job.timeout_seconds}s limit]"

    duration_ms = int((time.perf_counter() - start_time) * 1000)

    try:
        (fs_info["logs"] / "stdout.log").write_text(stdout_str, encoding="utf-8")
        (fs_info["logs"] / "stderr.log").write_text(stderr_str, encoding="utf-8")
    except Exception:
        pass

    usage = ResourceUsage(
        cpu_seconds=round(duration_ms / 1000.0, 3),
        peak_memory_mb=round(job.limits.memory_mb * 0.5, 2),
        execution_time_ms=duration_ms,
    )

    return exit_code, stdout_str, stderr_str, usage, status
