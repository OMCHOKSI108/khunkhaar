"""Evidence persistence.

Reports and evidence must survive between executions (spec section 2), so every artifact is
written under evidence/ or reports/ inside a run-scoped directory and recorded in the metadata
store. Anything written here is passed through redaction first.

The metadata row is a convenience index for the dashboard and the retention policy; writing an
artifact never depends on it succeeding, so a locked or unreadable database cannot lose evidence.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from . import paths
from .redaction import redact_obj

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")

# Retention classes are derived from the artifact, not from caller-supplied text.
_CLASS_BY_TOKEN = (
    ("screenshot", "screenshot"),
    ("trace", "trace"),
    ("video", "video"),
    ("har", "network"),
    ("a11y", "accessibility"),
    ("axe", "accessibility"),
    ("verification", "verification"),
    ("report", "report"),
)


def classify(relative_path: str) -> str:
    lowered = (relative_path or "").lower()
    for token, label in _CLASS_BY_TOKEN:
        if token in lowered:
            return label
    return "probe"


def new_run_id(prefix: str = "run") -> str:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{stamp}"


def slugify(text: str, max_len: int = 60) -> str:
    safe = _SAFE.sub("-", (text or "").strip())[:max_len]
    return safe.strip("-") or "unnamed"


def evidence_root(*parts: str) -> Path:
    return paths.evidence_dir(*parts)


def _register(target: Path, *, kind: str | None, project_id: str | None,
              run_id: str | None) -> None:
    try:
        from core.metadata import store  # imported late: evidence writing must not need the DB

        raw = target.read_bytes()
        store.add_evidence_ref(
            project_id, run_id, kind or target.suffix.lstrip(".") or "file", relative(target),
            len(raw), sha256=hashlib.sha256(raw).hexdigest(),
            retention_class=classify(relative(target)),
        )
    except Exception:  # noqa: B110 - evidence is the deliverable; the index must not break it
        pass  # nosec B110


def write_json(dir_path: Path, name: str, payload: Any, *, project_id: str | None = None,
               run_id: str | None = None, kind: str | None = None) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    target = dir_path / _SAFE.sub("-", name)
    target.write_text(
        json.dumps(redact_obj(payload), indent=2, default=str, ensure_ascii=False),
        encoding="utf-8",
    )
    target.chmod(0o600)
    _register(target, kind=kind, project_id=project_id, run_id=run_id)
    return target


def write_bytes(dir_path: Path, name: str, data: bytes, *, project_id: str | None = None,
                run_id: str | None = None, kind: str | None = None) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    target = dir_path / _SAFE.sub("-", name)
    target.write_bytes(data)
    target.chmod(0o600)
    _register(target, kind=kind, project_id=project_id, run_id=run_id)
    return target


def relative(path: Path) -> str:
    """Repo-relative reference, so evidence paths in the DB never leak absolute home paths."""
    try:
        return str(path.resolve().relative_to(paths.kh_root().resolve()))
    except ValueError:
        return str(path)


def cleanup_tmp(max_age_hours: int) -> list[str]:
    """Defined cleanup policy: only runtime/tmp is auto-pruned, never evidence or reports."""
    root = paths.tmp_dir()
    removed: list[str] = []
    cutoff = dt.datetime.now().timestamp() - max_age_hours * 3600
    if not root.exists():
        return removed
    for item in sorted(root.rglob("*")):
        if item.is_file() and item.stat().st_mtime < cutoff:
            item.unlink(missing_ok=True)
            removed.append(relative(item))
    for item in sorted(root.rglob("*"), reverse=True):
        if item.is_dir() and not any(item.iterdir()):
            item.rmdir()
    return removed
