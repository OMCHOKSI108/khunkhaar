"""Codebase path resolution utility supporting host and container environments."""
from __future__ import annotations

import os
from pathlib import Path

# The host directory bind-mounted into the container at /workspace (see docker-compose.yml's
# WORKSPACE_DIR). Defaults to the previous hardcoded path for existing installs.
HOST_WORKSPACE = os.environ.get("KHUNKHAAR_HOST_WORKSPACE", str(Path.home() / "workspace"))
CONTAINER_WORKSPACE = "/workspace"


def resolve_codebase_path(raw_path: str | None) -> Path | None:
    """Resolve codebase directory, handling container-to-host path mappings."""
    if not raw_path:
        return None

    cleaned = raw_path.strip()
    if not cleaned:
        return None

    candidate = Path(cleaned).expanduser()
    if candidate.is_dir():
        return candidate

    # Container mapping 1: any host path containing /workspace/ -> /workspace/
    if "/workspace/" in cleaned:
        rel = cleaned.split("/workspace/", 1)[1].lstrip("/")
        docker_candidate = Path(CONTAINER_WORKSPACE) / rel
        if docker_candidate.is_dir():
            return docker_candidate

    if cleaned.endswith("/workspace"):
        docker_candidate = Path(CONTAINER_WORKSPACE)
        if docker_candidate.is_dir():
            return docker_candidate

    # Container mapping 2: host workspace path -> /workspace
    hw = os.path.expanduser(HOST_WORKSPACE)
    if cleaned.startswith(hw):
        rel = cleaned[len(hw):].lstrip("/")
        docker_candidate = Path(CONTAINER_WORKSPACE) / rel
        if docker_candidate.is_dir():
            return docker_candidate

    # Container mapping 3: /workspace -> host workspace path
    if cleaned.startswith(CONTAINER_WORKSPACE):
        rel = cleaned[len(CONTAINER_WORKSPACE):].lstrip("/")
        host_candidate = Path(hw) / rel
        if host_candidate.is_dir():
            return host_candidate

    # Relative to current working directory
    cwd_candidate = Path.cwd() / cleaned
    if cwd_candidate.is_dir():
        return cwd_candidate

    return None

