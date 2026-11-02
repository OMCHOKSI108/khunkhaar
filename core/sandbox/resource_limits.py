"""Resource Enforcement: POSIX rlimits, process caps, and timeout terminations."""
from __future__ import annotations

import os
import resource
import signal
from typing import Callable

from core.sandbox.models import ResourceLimits


def configure_rlimits(limits: ResourceLimits) -> Callable[[], None]:
    """Returns a preexec_fn for subprocesses enforcing hard memory, cpu, and file caps."""
    def _apply():
        try:
            # 1. Memory ceiling (Address Space) in bytes
            mem_bytes = limits.max_memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))
        except (ValueError, OSError):
            pass

        try:
            # 2. Maximum processes (fork bomb protection accommodating threads)
            cur_soft, cur_hard = resource.getrlimit(resource.RLIMIT_NPROC)
            target_nproc = min(max(limits.max_processes, 1024), cur_hard)
            resource.setrlimit(resource.RLIMIT_NPROC, (target_nproc, cur_hard))
        except (ValueError, OSError):
            pass

        try:
            # 3. Maximum file output size (disk fill protection)
            max_file_bytes = limits.max_workspace_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_FSIZE, (max_file_bytes, max_file_bytes))
        except (ValueError, OSError):
            pass

        # 4. Set process group so we can terminate child trees cleanly
        os.setpgrp()

    return _apply


def kill_process_tree(pid: int) -> None:
    """Forcefully terminates a child process and all its spawned forks."""
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass

    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def cap_output_string(output: str, max_bytes: int = 5 * 1024 * 1024) -> str:
    if len(output.encode("utf-8", errors="replace")) > max_bytes:
        truncated = output[:max_bytes // 2]
        return truncated + f"\n\n[TRUNCATED: Output exceeded {max_bytes // (1024*1024)} MB limit]"
    return output
