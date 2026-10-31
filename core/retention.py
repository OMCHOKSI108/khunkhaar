"""Evidence index and retention policy.

Phase 1 pruned only `runtime/tmp`. Phase 3 adds a real index (`evidence_refs`) plus a retention
policy that is explicit about what it will delete and what it will never touch:

* `plan` is a dry run and prints bytes per class;
* `apply` needs `--confirm`, only deletes files under `evidence/` or `runtime/tmp`, and refuses any
  file that is still referenced by a finding that is not closed;
* reports are never deleted by retention.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.metadata import store
from core.shared import evidence as ev
from core.shared import paths

PROTECTED_STATUSES = ("confirmed", "likely", "unverified", "needs_human", "false_positive")
_ROOTS = ("evidence", "reports")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _relative(path: Path) -> str:
    return ev.relative(path)


def _project_by_run_id(conn) -> dict[str, str]:
    """Attribution that already exists in the database - never a guess from a filename."""
    mapping: dict[str, str] = {}
    for row in conn.execute("SELECT run_id, project_id FROM qa_runs WHERE project_id IS NOT NULL"):
        if row["run_id"]:
            mapping[row["run_id"]] = row["project_id"]
    for row in conn.execute("SELECT run_id, project_id FROM report_refs "
                            "WHERE project_id IS NOT NULL AND run_id IS NOT NULL"):
        mapping.setdefault(row["run_id"], row["project_id"])
    return mapping


def _project_by_path(conn) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for row in conn.execute("SELECT id, project_id, evidence_json, reproduction_json FROM findings "
                            "WHERE project_id IS NOT NULL"):
        for blob in (row["evidence_json"], row["reproduction_json"]):
            for ref in re.findall(r"(?:evidence|reports)/[^\"',}\s]+", str(blob or "")):
                mapping[ref] = row["project_id"]
    return mapping


def backfill_run_projects() -> int:
    """Attach project ids to historical `qa_runs` rows using `report_refs`, which Phase 2 did write.

    Phase 2 recorded runs with `project_id = NULL`, so per-project execution history was
    unrecoverable from that table alone. This joins on the run id instead of assuming a project.
    """
    with store.connect() as conn:
        runs = {row["run_id"]: row["project_id"] for row in
                conn.execute("SELECT run_id, project_id FROM report_refs "
                             "WHERE run_id IS NOT NULL AND project_id IS NOT NULL")}
        updated = 0
        for run_id, project_id in runs.items():
            cursor = conn.execute("UPDATE qa_runs SET project_id=? WHERE run_id=? "
                                  "AND project_id IS NULL", (project_id, run_id))
            updated += cursor.rowcount
    return updated


def index_all(*, roots: tuple[str, ...] = _ROOTS) -> dict[str, int]:
    """Register every artifact on disk that the metadata store does not know about yet.

    Existing Phase 1/2 evidence predates the index, so the dashboard and retention both work from
    a rebuilt index rather than trusting that writers recorded themselves.
    """
    with store.connect() as conn:
        known = {row["path"] for row in conn.execute("SELECT path FROM evidence_refs")}
        by_run = _project_by_run_id(conn)
        by_path = _project_by_path(conn)
        added = 0
        attributed = 0
        skipped = 0
        for root_name in roots:
            root = paths.kh_root() / root_name
            if not root.is_dir():
                continue
            for item in sorted(root.rglob("*")):
                if not item.is_file():
                    continue
                ref = _relative(item)
                if ref in known:
                    skipped += 1
                    continue
                raw = item.read_bytes()
                parts = Path(ref).parts
                run_id = next((part for part in reversed(parts)
                               if re.fullmatch(r"[A-Za-z0-9-]+-\d{8}-\d{6}", part)), None)
                project_id = by_path.get(ref) or (by_run.get(run_id) if run_id else None)
                if project_id is None:
                    project_id = next((part for part in parts
                                       if re.fullmatch(r"[a-z0-9-]{8,40}-[0-9a-f]{8}", part)), None)
                if project_id:
                    attributed += 1
                conn.execute(
                    "INSERT INTO evidence_refs (project_id, run_id, kind, path, bytes, sha256, "
                    "retention_class, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (project_id, run_id, item.suffix.lstrip(".") or "file", ref, len(raw),
                     hashlib.sha256(raw).hexdigest(), ev.classify(ref),
                     datetime.fromtimestamp(item.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")),
                )
                known.add(ref)
                added += 1
    return {"added": added, "already_indexed": skipped, "attributed_to_project": attributed}


def _open_finding_paths() -> set[str]:
    statuses = protected_statuses()
    with store.connect() as conn:
        rows = conn.execute("SELECT evidence_json, reproduction_json FROM findings "  # nosec B608
                            "WHERE status IN (%s)" % ",".join("?" * len(statuses)),
                            statuses).fetchall()  # placeholder count only; values are bound
    protected: set[str] = set()
    for row in rows:
        for blob in (row["evidence_json"], row["reproduction_json"]):
            if not blob:
                continue
            protected.update(re.findall(r"evidence/[^\"',}\s]+", str(blob)))
    return protected


def plan(*, days: int | None = None, by_class: dict[str, int] | None = None
         ) -> dict[str, Any]:
    """What retention would delete, without deleting anything."""
    windows = dict(by_class or {})
    cutoffs = {}
    for cls, window in windows.items():
        cutoffs[cls] = _now() - timedelta(days=window)
    protected = _open_finding_paths()
    candidates: list[dict[str, Any]] = []
    totals: dict[str, int] = {}
    kept: dict[str, int] = {}
    with store.connect() as conn:
        rows = conn.execute("SELECT * FROM evidence_refs").fetchall()
    for row in rows:
        ref = row["path"]
        cls = row["retention_class"] or ev.classify(ref)
        created = _parse(row["created_at"])
        window = windows.get(cls, days)
        eligible = window is not None and created is not None and created < cutoffs.get(
            cls, _now() - timedelta(days=window))
        absolute = paths.kh_root() / ref
        inside_scope = ref.startswith("evidence/") or ref.startswith("runtime/tmp/")
        if eligible and inside_scope and ref not in protected and absolute.is_file():
            candidates.append({"path": ref, "class": cls, "bytes": row["bytes"] or 0,
                               "created_at": row["created_at"]})
            totals[cls] = totals.get(cls, 0) + (row["bytes"] or 0)
        else:
            kept[cls] = kept.get(cls, 0) + 1
    return {"windows_days": windows, "default_days": days, "protected": len(protected),
            "candidates": candidates, "bytes_reclaimable": sum(totals.values()),
            "by_class_bytes": totals, "by_class_kept": kept}


def apply(*, confirm: bool, days: int | None = None,
          by_class: dict[str, int] | None = None) -> dict[str, Any]:
    scheduled = plan(days=days, by_class=by_class)
    if not confirm:
        return {"dry_run": True, "deleted": [], "would_delete": scheduled["candidates"],
                "bytes_reclaimable": scheduled["bytes_reclaimable"]}
    deleted: list[dict[str, Any]] = []
    for entry in scheduled["candidates"]:
        absolute = paths.kh_root() / entry["path"]
        try:
            absolute.unlink(missing_ok=True)
        except OSError:
            continue
        with store.connect() as conn:
            conn.execute("DELETE FROM evidence_refs WHERE path=?", (entry["path"],))
        deleted.append(entry)
    return {"dry_run": False, "deleted": deleted, "would_delete": [],
            "bytes_freed": sum(d["bytes"] for d in deleted)}


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# The classes a retention window may be configured for; `report` is absent because reports live
# outside the deletable roots and are never eligible.
WINDOWABLE_CLASSES = ("screenshot", "trace", "video", "network", "accessibility",
                      "verification", "probe")


def windows_from_config() -> tuple[int | None, dict[str, int]]:
    """Translate `retention.*` config keys into a default window and per-class overrides.

    `evidence_days: 0` means "no window at all", which keeps the shipped default non-destructive:
    a fresh install plans zero deletions until the operator sets a number.
    """
    from core.shared.config import current

    config = current()
    default = int(config.get("retention", "evidence_days", 0) or 0) or None
    by_class: dict[str, int] = {}
    for name in WINDOWABLE_CLASSES:
        value = int(config.get("retention", f"{name}_days", 0) or 0)
        if value > 0:
            by_class[name] = value
    return default, by_class


def protected_statuses() -> tuple[str, ...]:
    from core.shared.config import current

    return current().retention_protect_statuses() or PROTECTED_STATUSES
