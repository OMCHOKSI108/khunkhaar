"""Artifact Quarantine and Collection for sandbox runs."""
from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path
from typing import Any

MAX_TOTAL_ARTIFACTS_BYTES = 100 * 1024 * 1024
MAX_SINGLE_FILE_BYTES = 20 * 1024 * 1024


def compute_file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while chunk := f.read(65536):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return ""


def collect_execution_artifacts(artifacts_dir: Path) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    total_bytes = 0

    if not artifacts_dir.exists():
        return collected

    for fpath in artifacts_dir.rglob("*"):
        if not fpath.is_file():
            continue
        try:
            fsize = fpath.stat().st_size
            if fsize > MAX_SINGLE_FILE_BYTES:
                continue
            if total_bytes + fsize > MAX_TOTAL_ARTIFACTS_BYTES:
                break
            total_bytes += fsize

            mime, _ = mimetypes.guess_type(str(fpath))
            collected.append({
                "name": fpath.name,
                "relative_path": str(fpath.relative_to(artifacts_dir)),
                "storage_path": str(fpath),
                "file_size": fsize,
                "mime_type": mime or "application/octet-stream",
                "checksum": compute_file_sha256(fpath),
            })
        except Exception:
            continue

    return collected
