"""Filesystem Isolation: ephemeral workspace management and path sanitization."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from core.shared import paths

BASE_SANDBOX_DIR = paths.data_dir() / "sandbox" / "executions"


class PathTraversalAttempt(Exception):
    pass


def prepare_execution_filesystem(execution_id: str, script_content: str, script_filename: str) -> dict[str, Path]:
    exec_root = BASE_SANDBOX_DIR / execution_id
    input_dir = exec_root / "input"
    workspace_dir = exec_root / "workspace"
    output_dir = exec_root / "output"
    artifacts_dir = exec_root / "artifacts"
    logs_dir = exec_root / "logs"

    for d in (input_dir, workspace_dir, output_dir, artifacts_dir, logs_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Write script to input file
    script_path = input_dir / script_filename
    script_path.write_text(script_content, encoding="utf-8")
    try:
        os.chmod(script_path, 0o444)  # Immutable input
    except OSError:
        pass

    return {
        "root": exec_root,
        "input": input_dir,
        "workspace": workspace_dir,
        "output": output_dir,
        "artifacts": artifacts_dir,
        "logs": logs_dir,
        "script": script_path,
    }


def validate_safe_path(target_path: Path, base_dir: Path) -> Path:
    """Strictly ensures target path resolves inside base_dir and does not escape via symlink or .. traversal."""
    resolved_base = base_dir.resolve()
    resolved_target = target_path.resolve()
    try:
        resolved_target.relative_to(resolved_base)
    except ValueError:
        raise PathTraversalAttempt(f"Path escape attempt: '{target_path}' is outside '{base_dir}'")
    return resolved_target


def cleanup_execution_filesystem(execution_id: str) -> bool:
    exec_root = BASE_SANDBOX_DIR / execution_id
    if exec_root.exists():
        try:
            shutil.rmtree(exec_root, ignore_errors=True)
            return True
        except Exception:
            return False
    return False
