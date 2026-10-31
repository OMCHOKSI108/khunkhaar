"""Single source of truth for KHUNKHAAR filesystem locations.

Nothing in this codebase may hardcode a home directory. Every path is derived from
KH_ROOT, which callers can override with the KHUNKHAAR_ROOT environment variable.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_ENV_ROOT = "KHUNKHAAR_ROOT"


def kh_root() -> Path:
    override = os.environ.get(_ENV_ROOT)
    if override:
        root = Path(override).expanduser().resolve()
        if not root.is_dir():
            raise RuntimeError(f"{_ENV_ROOT}={root} is not an existing directory")
        return root
    # core/shared/paths.py -> core/shared -> core -> KHUNKHAAR-QA
    return Path(__file__).resolve().parents[2]


def _sub(*parts: str, create: bool = True) -> Path:
    path = kh_root().joinpath(*parts)
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def config_dir() -> Path:
    return _sub("config")


def config_file(name: str) -> Path:
    return kh_root() / "config" / name


def core_dir() -> Path:
    return _sub("core")


def data_dir() -> Path:
    return _sub("data")


def metadata_db() -> Path:
    _sub("data", "metadata")
    return kh_root() / "data" / "metadata" / "platform.sqlite3"


def profiles_dir() -> Path:
    return _sub("data", "profiles")


def state_dir() -> Path:
    return _sub("data", "state")


def registry_dir() -> Path:
    return _sub("projects", "registry")


def project_dir(project_id: str) -> Path:
    return _sub("projects", "registry", project_id)


def evidence_dir(*parts: str) -> Path:
    return _sub("evidence", *parts)


def reports_dir(*parts: str) -> Path:
    return _sub("reports", *parts)


def logs_dir() -> Path:
    return _sub("logs")


def runtime_dir(*parts: str) -> Path:
    return _sub("runtime", "tmp", *parts)


def tmp_dir() -> Path:
    return _sub("runtime", "tmp")


def bin_dir() -> Path:
    return _sub("bin")


def git_intelligence_dir(*parts: str) -> Path:
    return _sub("git-intelligence", *parts)


def regression_dir(*parts: str) -> Path:
    return _sub("regression", *parts)


def monitoring_dir(*parts: str) -> Path:
    return _sub("monitoring", *parts)


def intelligence_dir(*parts: str) -> Path:
    return _sub("intelligence", *parts)


def project_knowledge_dir(project_id: str) -> Path:
    return _sub("intelligence", "project-knowledge", slug_segment(project_id))


def global_knowledge_dir() -> Path:
    return _sub("intelligence", "global-knowledge")


def dependency_graphs_dir() -> Path:
    return _sub("intelligence", "dependency-graphs")


def project_state_dir(project_id: str) -> Path:
    return _sub("data", "project-state", slug_segment(project_id))


def execution_history_dir() -> Path:
    return _sub("data", "execution-history")


def dashboard_dir(*parts: str) -> Path:
    return _sub("dashboard", *parts)


def dashboard_secrets_dir() -> Path:
    return _sub("data", "profiles", "dashboard")


def slug_segment(value: str) -> str:
    """Path-safe project identifier. Project ids are hex-suffixed slugs already, but nothing that
    came from a filesystem path may be interpolated into a path unchecked."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", (value or "").strip())[:80].strip("-")
    if not safe:
        raise ValueError("empty path segment")
    if ".." in safe or safe.startswith("."):
        raise ValueError(f"path segment may not traverse or hide: {value!r}")
    return safe


def venv_bin() -> Path:
    return kh_root() / ".venv" / "bin"


def version() -> str:
    vf = kh_root() / "VERSION"
    return vf.read_text().strip() if vf.exists() else "0.0.0-unknown"
