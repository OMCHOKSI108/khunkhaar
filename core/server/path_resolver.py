"""Codebase path resolution utility supporting host and container environments."""
from __future__ import annotations

import os
from pathlib import Path


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

    # Container mapping 1: /home/omchoksi/workspace -> /workspace
    if cleaned.startswith("/home/omchoksi/workspace"):
        rel = cleaned[len("/home/omchoksi/workspace"):].lstrip("/")
        docker_candidate = Path("/workspace") / rel
        if docker_candidate.is_dir():
            return docker_candidate

    # Container mapping 2: /workspace -> /home/omchoksi/workspace
    if cleaned.startswith("/workspace"):
        rel = cleaned[len("/workspace"):].lstrip("/")
        host_candidate = Path("/home/omchoksi/workspace") / rel
        if host_candidate.is_dir():
            return host_candidate

    # Relative to current working directory
    cwd_candidate = Path.cwd() / cleaned
    if cwd_candidate.is_dir():
        return cwd_candidate

    return None
