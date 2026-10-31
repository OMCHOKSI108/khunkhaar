"""Persistent findings, invariants, missions, journeys and exploration history.

Lives in the existing platform SQLite database (data/metadata/platform.sqlite3) and extends it
with additive tables. One project's findings are scoped by project_id and never mixed; the
store exposes only project-filtered reads.
"""

from __future__ import annotations

import json
from typing import Any

from core.findings import models
from core.metadata.store import connect, init as init_platform
from core.shared import paths

__all__ = [
    "init", "ensure", "add_invariant", "list_invariants", "save_app_map", "latest_app_map",
    "add_finding", "get_finding", "list_findings", "update_status", "add_event",
    "list_events", "findings_stats", "save_journey", "list_journeys", "record_state",
    "upsert_mission", "list_missions",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_maps (
    project_id   TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    path         TEXT NOT NULL,
    summary_json TEXT,
    PRIMARY KEY (project_id, generated_at)
);

CREATE TABLE IF NOT EXISTS invariants (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL,
    rule_id     TEXT,
    description TEXT NOT NULL,
    source      TEXT NOT NULL,
    feature     TEXT,
    roles       TEXT,
    method      TEXT,
    confidence  REAL NOT NULL,
    entities    TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS missions (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL,
    kind        TEXT NOT NULL,
    title       TEXT NOT NULL,
    target      TEXT,
    spec_json   TEXT,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS journeys (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL,
    name        TEXT NOT NULL,
    steps_json  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS findings (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL,
    fingerprint     TEXT NOT NULL UNIQUE,
    feature         TEXT NOT NULL,
    category        TEXT NOT NULL,
    title           TEXT NOT NULL,
    description     TEXT NOT NULL,
    severity        TEXT NOT NULL,
    confidence      REAL NOT NULL,
    reproducibility TEXT NOT NULL,
    status          TEXT NOT NULL,
    role            TEXT,
    initial_state   TEXT,
    steps_json      TEXT,
    expected        TEXT,
    actual          TEXT,
    evidence_json   TEXT,
    remediation     TEXT,
    source_refs     TEXT,
    rule_ids        TEXT,
    invariant_id    TEXT,
    reproduction_json TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS finding_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    finding_id    TEXT NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
    at            TEXT NOT NULL,
    actor         TEXT NOT NULL,
    action        TEXT NOT NULL,
    note          TEXT,
    evidence_json TEXT
);

CREATE TABLE IF NOT EXISTS exploration_states (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    project_id   TEXT NOT NULL,
    state_hash   TEXT NOT NULL,
    url          TEXT,
    actions_json TEXT,
    created_at   TEXT NOT NULL
);
"""


def ensure(conn) -> None:
    """Create the Phase 2 tables and columns on an open connection. Idempotent, so the migration
    runner and `init()` share exactly one definition of this schema."""
    conn.executescript(_SCHEMA)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(findings)")}
    for name, kind in (("invariant_id", "TEXT"), ("reproduction_json", "TEXT")):
        if name not in columns:
            conn.execute(f"ALTER TABLE findings ADD COLUMN {name} {kind}")  # nosec B608


def init() -> None:
    init_platform()
    with connect() as conn:
        ensure(conn)
    paths.metadata_db().chmod(0o600)


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def add_invariant(project_id: str, invariant: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO invariants (id, project_id, rule_id, description, source, feature, roles, "
            "method, confidence, entities, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET description=excluded.description, "
            "source=excluded.source, confidence=excluded.confidence, method=excluded.method",
            (invariant["id"], project_id, invariant.get("rule_id"), invariant["description"],
             invariant["source"], invariant.get("feature"), _json(invariant.get("roles", [])),
             invariant.get("method"), float(invariant.get("confidence", 0.5)),
             _json(invariant.get("entities", [])), _now()),
        )


def list_invariants(project_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM invariants WHERE project_id=? ORDER BY id",
                            (project_id,)).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["roles"] = json.loads(item.get("roles") or "[]")
        item["entities"] = json.loads(item.get("entities") or "[]")
        out.append(item)
    return out


def save_app_map(project_id: str, path: str, summary: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO app_maps (project_id, generated_at, path, summary_json) VALUES (?,?,?,?)",
            (project_id, _now(), path, _json(summary)),
        )


def latest_app_map(project_id: str) -> dict[str, Any] | None:
    """The most recently generated application map for a project, with its summary parsed."""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM app_maps WHERE project_id=? ORDER BY generated_at DESC, rowid DESC "
            "LIMIT 1", (project_id,)).fetchone()
    if not row:
        return None
    item = dict(row)
    item["summary"] = json.loads(item.pop("summary_json") or "{}")
    return item


def upsert_mission(mission: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO missions (id, project_id, kind, title, target, spec_json, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status",
            (mission["id"], mission["project_id"], mission["kind"], mission["title"],
             mission.get("target"), _json(mission.get("spec", {})), mission.get("status", "pending"),
             _now()),
        )


def list_missions(project_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM missions WHERE project_id=? ORDER BY created_at",
                            (project_id,)).fetchall()
    return [dict(r) for r in rows]


def add_finding(finding: models.Finding, actor: str, note: str = "") -> tuple[dict[str, Any], bool]:
    """Insert or update by fingerprint. Returns (row, created)."""
    stamp = _now()
    with connect() as conn:
        existing = conn.execute("SELECT id FROM findings WHERE fingerprint=?",
                                (finding.fingerprint,)).fetchone()
        if existing:
            finding_id = existing["id"]
            conn.execute(
                "UPDATE findings SET description=?, severity=?, steps_json=?, expected=?, actual=?, "
                "evidence_json=?, source_refs=?, rule_ids=?, invariant_id=?, reproduction_json=?, "
                "updated_at=? WHERE id=?",
                (finding.description, finding.severity, _json(finding.steps), finding.expected,
                 finding.actual, _json(finding.evidence), _json(finding.source_refs),
                 _json(finding.rule_ids), finding.invariant_id, _json(finding.reproduction),
                 stamp, finding_id),
            )
            created = False
        else:
            finding_id = finding.finding_id
            conn.execute(
                "INSERT INTO findings (id, project_id, fingerprint, feature, category, title, "
                "description, severity, confidence, reproducibility, status, role, initial_state, "
                "steps_json, expected, actual, evidence_json, remediation, source_refs, rule_ids, "
                "invariant_id, reproduction_json, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (finding_id, finding.project_id, finding.fingerprint, finding.feature,
                 finding.category, finding.title, finding.description, finding.severity,
                 finding.confidence, finding.reproducibility, finding.status, finding.role,
                 finding.initial_state, _json(finding.steps), finding.expected, finding.actual,
                 _json(finding.evidence), finding.remediation, _json(finding.source_refs),
                 _json(finding.rule_ids), finding.invariant_id, _json(finding.reproduction),
                 stamp, stamp),
            )
            created = True
        conn.execute(
            "INSERT INTO finding_events (finding_id, at, actor, action, note, evidence_json) "
            "VALUES (?,?,?,?,?,?)",
            (finding_id, stamp, actor, "discovered" if created else "rediscovered",
             note or finding.title, _json(finding.evidence)),
        )
    return get_finding(finding_id), created


def get_finding(finding_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM findings WHERE id=?", (finding_id,)).fetchone()
    if not row:
        return None
    item = dict(row)
    for key, target in (("steps_json", "steps"), ("evidence_json", "evidence"),
                        ("source_refs", "source_refs"), ("rule_ids", "rule_ids"),
                        ("reproduction_json", "reproduction")):
        raw = item.pop(key) or ("{}" if target == "reproduction" else "[]")
        item[target] = json.loads(raw)
    return item


def list_findings(project_id: str, status: str | None = None,
                  category: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT id FROM findings WHERE project_id=?"
    params: list[Any] = [project_id]
    if status:
        sql += " AND status=?"
        params.append(status)
    if category:
        sql += " AND category=?"
        params.append(category)
    sql += " ORDER BY created_at DESC"
    with connect() as conn:
        ids = [r["id"] for r in conn.execute(sql, params).fetchall()]
    return [f for f in (get_finding(i) for i in ids) if f]


def update_status(finding_id: str, status: str, actor: str, note: str,
                  evidence: dict[str, Any] | None = None,
                  confidence: float | None = None,
                  reproducibility: str | None = None) -> dict[str, Any]:
    if status not in models.STATUSES:
        raise ValueError(f"unknown status '{status}'")
    stamp = _now()
    with connect() as conn:
        row = conn.execute("SELECT status FROM findings WHERE id=?", (finding_id,)).fetchone()
        if not row:
            raise KeyError(finding_id)
        new_confidence = confidence if confidence is not None else models.CONFIDENCE_BY_STATUS[status]
        conn.execute(
            "UPDATE findings SET status=?, confidence=?, reproducibility=COALESCE(?, reproducibility), "
            "updated_at=? WHERE id=?",
            (status, new_confidence, reproducibility, stamp, finding_id),
        )
        conn.execute(
            "INSERT INTO finding_events (finding_id, at, actor, action, note, evidence_json) "
            "VALUES (?,?,?,?,?,?)",
            (finding_id, stamp, actor, f"status:{status}", note, _json(evidence or {})),
        )
    return get_finding(finding_id)  # type: ignore[return-value]


def add_event(finding_id: str, actor: str, action: str, note: str,
              evidence: dict[str, Any] | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO finding_events (finding_id, at, actor, action, note, evidence_json) "
            "VALUES (?,?,?,?,?,?)",
            (finding_id, _now(), actor, action, note, _json(evidence or {})),
        )


def list_events(finding_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM finding_events WHERE finding_id=? ORDER BY id",
                            (finding_id,)).fetchall()
    return [dict(r) for r in rows]


def findings_stats(project_id: str) -> dict[str, Any]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT status, category, severity, count(*) AS n FROM findings WHERE project_id=? "
            "GROUP BY status, category, severity", (project_id,)).fetchall()
    by_status: dict[str, int] = {}
    by_category: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    for row in rows:
        by_status[row["status"]] = by_status.get(row["status"], 0) + row["n"]
        by_category[row["category"]] = by_category.get(row["category"], 0) + row["n"]
        by_severity[row["severity"]] = by_severity.get(row["severity"], 0) + row["n"]
    return {"total": sum(by_status.values()), "by_status": by_status,
            "by_category": by_category, "by_severity": by_severity}


def save_journey(project_id: str, journey_id: str, name: str, steps: list[dict[str, Any]]) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO journeys (id, project_id, name, steps_json, created_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, steps_json=excluded.steps_json",
            (journey_id, project_id, name, _json(steps), _now()),
        )


def list_journeys(project_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM journeys WHERE project_id=? ORDER BY created_at",
                            (project_id,)).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["steps"] = json.loads(item.pop("steps_json") or "[]")
        out.append(item)
    return out


def record_state(run_id: str, project_id: str, state_hash: str, url: str,
                 actions: list[dict[str, Any]]) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO exploration_states (run_id, project_id, state_hash, url, actions_json, "
            "created_at) VALUES (?,?,?,?,?,?)",
            (run_id, project_id, state_hash, url, _json(actions), _now()),
        )
