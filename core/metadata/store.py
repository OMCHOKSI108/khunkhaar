"""Central metadata store (SQLite).

Holds platform state only: registered projects, discovery snapshots, tool health, QA run ids and
references to reports/evidence. Secrets are never stored here - credentials stay in the 0600
profile files or the environment, referenced by name.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from core.metadata import migrations
from core.shared import paths

SCHEMA_VERSION = migrations.LATEST_VERSION

_SCHEMA_DOC = """Schema lives in core/metadata/migrations.py as an ordered list of versions.
This module deliberately contains no DDL: `init()` upgrades the database through migrations."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    db = paths.metadata_db()
    conn = sqlite3.connect(db, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init() -> None:
    migrations.upgrade()
    paths.metadata_db().chmod(0o600)


def schema_status() -> dict[str, Any]:
    return migrations.status()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def upsert_project(project_id: str, name: str, root_path: str, profile_path: str,
                   languages: list[str], frameworks: list[str], project_type: str) -> None:
    stamp = _now()
    with connect() as conn:
        conn.execute(
            "INSERT INTO projects (project_id, name, root_path, profile_path, first_seen, last_seen, "
            "languages, frameworks, project_type) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(project_id) DO UPDATE SET name=excluded.name, root_path=excluded.root_path, "
            "profile_path=excluded.profile_path, last_seen=excluded.last_seen, "
            "languages=excluded.languages, frameworks=excluded.frameworks, project_type=excluded.project_type",
            (project_id, name, root_path, profile_path, stamp, stamp,
             _json(languages), _json(frameworks), project_type),
        )
        conn.execute(
            "INSERT INTO discovery_snapshots (project_id, created_at, profile_path, summary) VALUES (?,?,?,?)",
            (project_id, stamp, profile_path,
             _json({"languages": languages, "frameworks": frameworks, "project_type": project_type})),
        )


def get_project(project_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
    return dict(row) if row else None


def list_projects() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM projects ORDER BY last_seen DESC").fetchall()
    return [dict(r) for r in rows]


def list_qa_runs(project_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    sql = "SELECT * FROM qa_runs"
    params: list[Any] = []
    if project_id:
        sql += " WHERE project_id=?"
        params.append(project_id)
    sql += " ORDER BY started_at DESC, run_id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 1000)))
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def record_tool_health(tool: str, status: str, version: str | None, detail: str | None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO tool_health (tool, status, version, detail, checked_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(tool) DO UPDATE SET status=excluded.status, version=excluded.version, "
            "detail=excluded.detail, checked_at=excluded.checked_at",
            (tool, status, version, detail, _now()),
        )


def tool_health() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM tool_health ORDER BY tool").fetchall()
    return [dict(r) for r in rows]


def start_run(run_id: str, project_id: str | None, kind: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO qa_runs (run_id, project_id, kind, status, started_at) VALUES (?,?,?,?,?)",
            (run_id, project_id, kind, "running", _now()),
        )


def finish_run(run_id: str, status: str, note: str | None = None, *,
               peak_rss_kb: int | None = None, llm_usage: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE qa_runs SET status=?, finished_at=?, note=?, peak_rss_kb=COALESCE(?, "
            "peak_rss_kb), llm_usage=COALESCE(?, llm_usage) WHERE run_id=?",
            (status, _now(), note, peak_rss_kb, llm_usage, run_id))


def get_run(run_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM qa_runs WHERE run_id=?", (run_id,)).fetchone()
    return dict(row) if row else None


def add_report_ref(project_id: str | None, run_id: str | None, path: str) -> None:
    with connect() as conn:
        conn.execute("INSERT INTO report_refs (project_id, run_id, path, created_at) VALUES (?,?,?,?)",
                     (project_id, run_id, path, _now()))


def add_evidence_ref(project_id: str | None, run_id: str | None, kind: str, path: str,
                     size: int | None, *, sha256: str | None = None,
                     retention_class: str | None = None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO evidence_refs (project_id, run_id, kind, path, bytes, sha256, "
            "retention_class, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (project_id, run_id, kind, path, size, sha256, retention_class, _now()),
        )


def list_report_refs(project_id: str | None = None, run_id: str | None = None,
                     limit: int = 100) -> list[dict[str, Any]]:
    sql = "SELECT * FROM report_refs WHERE 1=1"
    params: list[Any] = []
    if project_id:
        sql += " AND project_id=?"
        params.append(project_id)
    if run_id:
        sql += " AND run_id=?"
        params.append(run_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 1000)))
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def list_evidence_refs(project_id: str | None = None, run_id: str | None = None, *,
                       kind: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    sql = "SELECT * FROM evidence_refs WHERE 1=1"
    params: list[Any] = []
    if project_id:
        sql += " AND project_id=?"
        params.append(project_id)
    if run_id:
        sql += " AND run_id=?"
        params.append(run_id)
    if kind:
        sql += " AND kind=?"
        params.append(kind)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 5000)))
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def get_evidence_ref(ref_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM evidence_refs WHERE id=?", (int(ref_id),)).fetchone()
    return dict(row) if row else None


def record_git_analysis(project_id: str, run_id: str | None, kind: str, *,
                        base_ref: str | None, head_ref: str | None, summary: dict[str, Any],
                        artifact: str | None) -> int:
    """Store one git analysis and the evidence file that holds its detail.

    Only counts go in the row. The file list, the per-function attribution and every diff hunk
    stay in the artifact, so the metadata database never grows a copy of repository content.
    """
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO git_analyses (project_id, run_id, kind, base_ref, head_ref, commits, "
            "files_changed, insertions, deletions, renames, removed, conflicted, worktree_clean, "
            "index_untouched, artifact, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (project_id, run_id, kind, base_ref, head_ref, summary.get("commits", 0),
             summary.get("files_changed", 0), summary.get("insertions", 0),
             summary.get("deletions", 0), summary.get("renames", 0), summary.get("removed", 0),
             1 if summary.get("conflicted") else 0, 1 if summary.get("worktree_clean") else 0,
             1 if summary.get("index_untouched", True) else 0, artifact, _now()),
        )
        return int(cursor.lastrowid or 0)


def git_analyses(project_id: str, limit: int = 20) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 500))
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM git_analyses WHERE project_id=? ORDER BY id DESC LIMIT ?",
            (project_id, limit)).fetchall()
    return [dict(r) for r in rows]


def latest_git_analysis(project_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM git_analyses WHERE project_id=? ORDER BY id DESC LIMIT 1",
            (project_id,)).fetchone()
    return dict(row) if row else None


# Graph ids are project-relative by design; the storage key carries the project so two registered
# repositories with identical relative paths can never share a node row.
_IMPACT_SEPARATOR = "|"


def impact_storage_key(project_id: str, node_id: str) -> str:
    return f"{project_id}{_IMPACT_SEPARATOR}{node_id}"


def _impact_local_id(project_id: str, value: str) -> str:
    prefix = f"{project_id}{_IMPACT_SEPARATOR}"
    return value[len(prefix):] if value.startswith(prefix) else value


def replace_impact_graph(project_id: str, nodes: list[dict[str, Any]],
                         edges: list[dict[str, Any]]) -> dict[str, int]:
    """Replace one project's graph atomically. Rows for other projects are never touched."""
    stamp = _now()
    node_rows = [(
        impact_storage_key(project_id, node["node_id"]), project_id, node["type"], node["name"],
        node.get("path"), _json(node.get("meta") or {}), node.get("confidence", 1.0),
        node.get("origin", "explicit"), stamp,
    ) for node in nodes]
    edge_rows = [(
        project_id, impact_storage_key(project_id, edge["src"]),
        impact_storage_key(project_id, edge["dst"]), edge["relation"],
        edge.get("confidence", 1.0), edge.get("evidence", ""),
        edge.get("origin", "explicit"), stamp,
    ) for edge in edges]
    with connect() as conn:
        conn.execute("DELETE FROM impact_edges WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM impact_nodes WHERE project_id=?", (project_id,))
        conn.executemany(
            "INSERT INTO impact_nodes (node_id, project_id, type, name, path, meta, confidence, "
            "origin, updated_at) VALUES (?,?,?,?,?,?,?,?,?)", node_rows)
        conn.executemany(
            "INSERT INTO impact_edges (project_id, src, dst, relation, confidence, evidence, "
            "origin, updated_at) VALUES (?,?,?,?,?,?,?,?)", edge_rows)
    return {"nodes": len(node_rows), "edges": len(edge_rows)}


def load_impact_graph(project_id: str) -> dict[str, Any]:
    """Nodes and edges for one project only, with local (unprefixed) ids restored."""
    with connect() as conn:
        node_rows = conn.execute(
            "SELECT * FROM impact_nodes WHERE project_id=? ORDER BY node_id",
            (project_id,)).fetchall()
        edge_rows = conn.execute(
            "SELECT * FROM impact_edges WHERE project_id=? ORDER BY src, dst, relation",
            (project_id,)).fetchall()
    nodes = [{"node_id": _impact_local_id(project_id, row["node_id"]),
              "type": row["type"], "name": row["name"], "path": row["path"],
              "meta": json.loads(row["meta"] or "{}"),
              "confidence": row["confidence"], "origin": row["origin"] or "explicit"}
             for row in node_rows]
    edges = [{"src": _impact_local_id(project_id, row["src"]),
              "dst": _impact_local_id(project_id, row["dst"]),
              "relation": row["relation"], "confidence": row["confidence"],
              "evidence": row["evidence"] or "", "origin": row["origin"] or "explicit"}
             for row in edge_rows]
    updated = max((row["updated_at"] for row in node_rows), default=None)
    return {"nodes": nodes, "edges": edges, "updated_at": updated}


def impact_graph_info(project_id: str) -> dict[str, Any]:
    with connect() as conn:
        node_count = conn.execute("SELECT count(*) AS n FROM impact_nodes WHERE project_id=?",
                                  (project_id,)).fetchone()["n"]
        edge_count = conn.execute("SELECT count(*) AS n FROM impact_edges WHERE project_id=?",
                                  (project_id,)).fetchone()["n"]
        updated = conn.execute("SELECT MAX(updated_at) AS at FROM impact_nodes WHERE project_id=?",
                               (project_id,)).fetchone()["at"]
    return {"nodes": node_count, "edges": edge_count, "updated_at": updated}


def record_impact_report(project_id: str, *, analysis_id: int | None, base_ref: str | None,
                         head_ref: str | None, risk_score: float,
                         risk_factors: list[dict[str, Any]], artifact: str | None) -> str:
    report_id = f"IR-{secrets.token_hex(8)}"
    with connect() as conn:
        conn.execute(
            "INSERT INTO impact_reports (report_id, project_id, analysis_id, base_ref, head_ref, "
            "risk_score, risk_factors, artifact, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (report_id, project_id, analysis_id, base_ref, head_ref, risk_score,
             _json(risk_factors), artifact, _now()))
    return report_id


def latest_impact_report(project_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM impact_reports WHERE project_id=? ORDER BY created_at DESC LIMIT 1",
            (project_id,)).fetchone()
    if not row:
        return None
    item = dict(row)
    item["risk_factors"] = json.loads(item.get("risk_factors") or "[]")
    return item


def list_impact_reports(project_id: str, limit: int = 20) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM impact_reports WHERE project_id=? ORDER BY created_at DESC, rowid DESC "
            "LIMIT ?", (project_id, max(1, min(int(limit), 200)))).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["risk_factors"] = json.loads(item.get("risk_factors") or "[]")
        out.append(item)
    return out


def last_green_regression_run(project_id: str) -> dict[str, Any] | None:
    """Most recent passed regression run for the project, if any has been recorded yet."""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM regression_runs WHERE project_id=? AND result='pass' "
            "ORDER BY finished_at DESC, id DESC LIMIT 1", (project_id,)).fetchone()
    return dict(row) if row else None


_REGRESSION_TEST_FIELDS = (
    "project_id", "finding_id", "category", "method", "objective", "preconditions", "role",
    "steps", "expected", "actual", "dependencies", "reliability", "confidence",
    "generated_path", "validated_path", "last_success_at", "last_failure_at",
)


def upsert_regression_test(record: dict[str, Any]) -> dict[str, Any]:
    """Insert a generated test or refresh its definition without erasing its run history."""
    stamp = _now()
    with connect() as conn:
        existing = conn.execute("SELECT test_id FROM regression_tests WHERE test_id=?",
                                (record["test_id"],)).fetchone()
        if existing:
            conn.execute(
                "UPDATE regression_tests SET category=?, method=?, objective=?, preconditions=?, "
                "role=?, steps=?, expected=?, actual=?, dependencies=?, reliability=?, "
                "confidence=?, generated_path=?, validated_path=?, updated_at=? WHERE test_id=?",
                (record["category"], record["method"], record["objective"],
                 record.get("preconditions"), record.get("role"), _json(record.get("steps") or []),
                 record.get("expected"), record.get("actual"), _json(record.get("dependencies") or {}),
                 record.get("reliability", "generated"), float(record.get("confidence") or 0.0),
                 record.get("generated_path"), record.get("validated_path"), stamp,
                 record["test_id"]),
            )
        else:
            conn.execute(
                "INSERT INTO regression_tests (test_id, project_id, finding_id, category, method, "
                "objective, preconditions, role, steps, expected, actual, dependencies, reliability, "
                "confidence, generated_path, validated_path, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (record["test_id"], record["project_id"], record["finding_id"],
                 record["category"], record["method"], record["objective"],
                 record.get("preconditions"), record.get("role"), _json(record.get("steps") or []),
                 record.get("expected"), record.get("actual"),
                 _json(record.get("dependencies") or {}), record.get("reliability", "generated"),
                 float(record.get("confidence") or 0.0), record.get("generated_path"),
                 record.get("validated_path"), stamp, stamp),
            )
    return get_regression_test(record["test_id"])  # type: ignore[return-value]


def get_regression_test(test_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM regression_tests WHERE test_id=?", (test_id,)).fetchone()
    return _regression_test_row(row) if row else None


def list_regression_tests(project_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM regression_tests WHERE project_id=? "
                            "ORDER BY created_at, test_id", (project_id,)).fetchall()
    return [_regression_test_row(row) for row in rows]


def _regression_test_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["steps"] = json.loads(item.pop("steps") or "[]")
    item["dependencies"] = json.loads(item.pop("dependencies") or "{}")
    return item


def update_regression_test(test_id: str, **fields: Any) -> dict[str, Any]:
    allowed = {"reliability", "confidence", "generated_path", "validated_path",
               "last_success_at", "last_failure_at"}
    updates = {key: value for key, value in fields.items() if key in allowed}
    if not updates:
        return get_regression_test(test_id)  # type: ignore[return-value]
    assignments = ", ".join(f"{key}=?" for key in updates)
    with connect() as conn:
        conn.execute(f"UPDATE regression_tests SET {assignments}, updated_at=? WHERE test_id=?",
                     (*updates.values(), _now(), test_id))
    return get_regression_test(test_id)  # type: ignore[return-value]


def record_regression_run(project_id: str, *, test_id: str, result: str, run_id: str | None,
                          base_ref: str | None, head_ref: str | None, duration_ms: int,
                          note: str, evidence: str | None) -> dict[str, Any]:
    """Append one execution and move the test's confidence in the direction the run supports.

    `environment_unavailable` and `not_run` never lower confidence: a target that was down is not
    evidence about the application, so it must not look like a failed regression check.
    """
    started = _now()
    with connect() as conn:
        current = conn.execute("SELECT confidence, last_success_at, last_failure_at "
                               "FROM regression_tests WHERE test_id=?", (test_id,)).fetchone()
        confidence = float(current["confidence"] or 0.0) if current else 0.0
        if result == "pass":
            confidence = min(0.99, confidence + 0.03)
        elif result == "fail":
            confidence = max(0.1, confidence - 0.10)
        conn.execute(
            "INSERT INTO regression_runs (test_id, project_id, run_id, base_ref, head_ref, "
            "started_at, finished_at, result, duration_ms, note, evidence) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (test_id, project_id, run_id, base_ref, head_ref, started, _now(), result,
             duration_ms, note, evidence),
        )
        if result == "pass":
            conn.execute("UPDATE regression_tests SET last_success_at=?, confidence=?, "
                         "updated_at=? WHERE test_id=?", (started, confidence, started, test_id))
        elif result == "fail":
            conn.execute("UPDATE regression_tests SET last_failure_at=?, confidence=?, "
                         "updated_at=? WHERE test_id=?", (started, confidence, started, test_id))
    with connect() as conn:
        row = conn.execute("SELECT * FROM regression_runs WHERE test_id=? ORDER BY id DESC LIMIT 1",
                           (test_id,)).fetchone()
    return dict(row) if row else {}


def list_regression_runs(project_id: str, test_id: str | None = None,
                         limit: int = 50) -> list[dict[str, Any]]:
    sql = "SELECT * FROM regression_runs WHERE project_id=?"
    params: list[Any] = [project_id]
    if test_id:
        sql += " AND test_id=?"
        params.append(test_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, limit))
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def regression_stats(project_id: str) -> dict[str, Any]:
    """Counts the dashboard may show. `environment_unavailable` stays separate from `fail`."""
    with connect() as conn:
        test_rows = conn.execute(
            "SELECT reliability, count(*) AS n FROM regression_tests WHERE project_id=? "
            "GROUP BY reliability", (project_id,)).fetchall()
        run_rows = conn.execute(
            "SELECT result, count(*) AS n FROM regression_runs WHERE project_id=? "
            "GROUP BY result", (project_id,)).fetchall()
        latest = conn.execute(
            "SELECT * FROM regression_runs WHERE project_id=? ORDER BY id DESC LIMIT 1",
            (project_id,)).fetchone()
    by_reliability = {row["reliability"]: row["n"] for row in test_rows}
    by_result = {row["result"]: row["n"] for row in run_rows}
    return {
        "tests": sum(by_reliability.values()),
        "by_reliability": by_reliability,
        "by_result": by_result,
        "latest": dict(latest) if latest else None,
    }


def upsert_knowledge_item(record: dict[str, Any]) -> dict[str, Any]:
    """Insert or refresh a knowledge index row.

    The row never contains the knowledge body: it points at the 0600 artifact written by
    core.knowledge, so the database stays an index and the artifact stays reviewable on disk.
    """
    scope = record.get("scope")
    project_id = record.get("project_id")
    if scope not in ("global", "project"):
        raise ValueError(f"unknown knowledge scope '{scope}'")
    if scope == "project" and not project_id:
        raise ValueError("project-scoped knowledge requires a project_id")
    if scope == "global" and project_id:
        raise ValueError("global knowledge must not carry a project_id")
    stamp = _now()
    tags = _json(record.get("tags") or [])
    with connect() as conn:
        existing = conn.execute("SELECT item_id FROM knowledge_items WHERE item_id=?",
                                (record["item_id"],)).fetchone()
        if existing:
            conn.execute(
                "UPDATE knowledge_items SET kind=?, title=?, body_path=?, tags=?, updated_at=? "
                "WHERE item_id=?",
                (record["kind"], record["title"], record.get("body_path"), tags, stamp,
                 record["item_id"]),
            )
        else:
            conn.execute(
                "INSERT INTO knowledge_items (item_id, scope, project_id, kind, title, body_path, "
                "tags, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (record["item_id"], scope, project_id, record["kind"], record["title"],
                 record.get("body_path"), tags, stamp, stamp),
            )
    return get_knowledge_item(record["item_id"])  # type: ignore[return-value]


def _knowledge_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["tags"] = json.loads(item.get("tags") or "[]")
    return item


def get_knowledge_item(item_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM knowledge_items WHERE item_id=?", (item_id,)).fetchone()
    return _knowledge_row(row) if row else None


def list_knowledge_items(scope: str, project_id: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM knowledge_items WHERE scope=?"
    params: list[Any] = [scope]
    if scope == "project":
        sql += " AND project_id=?"
        params.append(project_id)
    sql += " ORDER BY kind, title, item_id"
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_knowledge_row(row) for row in rows]


def record_qa_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Insert or replace a plan header and its action rows.

    The artifact is the reviewed document; these rows exist so `plan list`, the dashboard and a
    later audit can answer who approved what without parsing files. Re-recording a plan replaces
    its derived action rows, never its approval.
    """
    plan_id = str(plan["plan_id"])
    summary = _json(plan.get("summary") or {})
    stamp = plan.get("created_at") or _now()
    with connect() as conn:
        existing = conn.execute(
            "SELECT plan_id, approved_at, approved_by, status FROM qa_plans WHERE plan_id=?",
            (plan_id,)).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO qa_plans (plan_id, project_id, run_id, mode, digest, status, "
                "approved_at, approved_by, summary, artifact, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (plan_id, plan["project_id"], plan.get("run_id"), plan["mode"], plan["digest"],
                 plan.get("status") or "draft", plan.get("approved_at"), plan.get("approved_by"),
                 summary, plan.get("artifact"), stamp))
        else:
            conn.execute(
                "UPDATE qa_plans SET project_id=?, run_id=?, mode=?, digest=?, status=?, "
                "summary=?, artifact=? WHERE plan_id=?",
                (plan["project_id"], plan.get("run_id"), plan["mode"], plan["digest"],
                 plan.get("status") or existing["status"], summary, plan.get("artifact"), plan_id))
        conn.execute("DELETE FROM plan_actions WHERE plan_id=?", (plan_id,))
        for action in plan.get("actions") or []:
            conn.execute(
                "INSERT INTO plan_actions (plan_id, project_id, agent, kind, feature, target, "
                "rationale, est_requests, est_steps, est_sessions, requires_approval, "
                "selected_tests) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (plan_id, plan["project_id"], str(action.get("agent") or "planner"),
                 str(action.get("kind") or "mission"), action.get("feature"),
                 action.get("target"), _json(action.get("rationale") or {}),
                 int((action.get("cost") or {}).get("requests") or 0),
                 int((action.get("cost") or {}).get("steps") or 0),
                 int((action.get("cost") or {}).get("sessions") or 0),
                 1 if action.get("requires_approval") else 0,
                 _json(action.get("selected_tests") or [])))
    return get_qa_plan(plan_id)


def get_qa_plan(plan_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM qa_plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            return None
        action_rows = conn.execute(
            "SELECT * FROM plan_actions WHERE plan_id=? ORDER BY id", (plan_id,)).fetchall()
    plan = dict(row)
    plan["summary"] = json.loads(plan.get("summary") or "{}")
    plan["actions"] = []
    for action_row in action_rows:
        action = dict(action_row)
        action["rationale"] = json.loads(action.get("rationale") or "{}")
        action["selected_tests"] = json.loads(action.get("selected_tests") or "[]")
        action["requires_approval"] = bool(action.get("requires_approval"))
        plan["actions"].append(action)
    return plan


def list_qa_plans(project_id: str, limit: int = 20) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM qa_plans WHERE project_id=? ORDER BY created_at DESC, plan_id DESC "
            "LIMIT ?", (project_id, max(1, int(limit)))).fetchall()
    out = []
    for row in rows:
        plan = dict(row)
        plan["summary"] = json.loads(plan.get("summary") or "{}")
        out.append(plan)
    return out


def update_qa_plan(plan_id: str, **fields: Any) -> dict[str, Any] | None:
    allowed = {"run_id", "status", "approved_at", "approved_by", "summary", "artifact", "digest"}
    updates = {key: value for key, value in fields.items() if key in allowed}
    if updates:
        if "summary" in updates:
            updates["summary"] = _json(updates["summary"])
        assignments = ", ".join(f"{key}=?" for key in updates)
        with connect() as conn:
            conn.execute(f"UPDATE qa_plans SET {assignments} WHERE plan_id=?",  # nosec B608
                         (*updates.values(), plan_id))
    return get_qa_plan(plan_id)


def grant_authorization(project_id: str, target: str, mode: str, *, granted_by: str,
                        expires_at: str | None = None, note: str | None = None) -> None:
    """Record explicit permission to test a target.

    Registering a repository only makes it analysable. Nothing in Phase 3 treats a row in
    `projects` as permission to send requests or run probes against a live system - that has to
    appear here, with the operator's name on it.
    """
    with connect() as conn:
        conn.execute(
            "INSERT INTO authorizations (project_id, target, mode, granted_at, granted_by, "
            "expires_at, note) VALUES (?,?,?,?,?,?,?)",
            (project_id, target, mode, _now(), granted_by, expires_at, note),
        )


def active_authorizations(project_id: str | None = None) -> list[dict[str, Any]]:
    now = _now()
    sql = "SELECT * FROM authorizations WHERE (expires_at IS NULL OR expires_at > ?)"
    params: list[Any] = [now]
    if project_id:
        sql += " AND project_id = ?"
        params.append(project_id)
    with connect() as conn:
        rows = conn.execute(sql + " ORDER BY granted_at DESC", params).fetchall()
    return [dict(r) for r in rows]


def is_authorized(project_id: str, target: str, mode: str) -> bool:
    """True when an unexpired grant covers this exact project/target/mode."""
    return any(a["project_id"] == project_id and a["target"] == target and a["mode"] == mode
               for a in active_authorizations(project_id))


def upsert_monitoring_schedule(record: dict[str, Any]) -> dict[str, Any]:
    stamp = _now()
    with connect() as conn:
        existing = conn.execute("SELECT schedule_id FROM monitoring_schedules WHERE schedule_id=?",
                                (record["schedule_id"],)).fetchone()
        if existing:
            conn.execute(
                "UPDATE monitoring_schedules SET project_id=?, name=?, expression=?, mode=?, "
                "target=?, checks=?, enabled=?, paused_at=?, last_due_at=?, last_run_at=?, "
                "next_due_at=?, artifact=? WHERE schedule_id=?",
                (record["project_id"], record["name"], record["expression"], record["mode"],
                 record.get("target"), _json(record.get("checks") or []),
                 int(bool(record.get("enabled", True))), record.get("paused_at"),
                 record.get("last_due_at"), record.get("last_run_at"),
                 record.get("next_due_at"), record.get("artifact"), record["schedule_id"]),
            )
        else:
            conn.execute(
                "INSERT INTO monitoring_schedules (schedule_id, project_id, name, expression, "
                "mode, target, checks, enabled, created_at, paused_at, last_due_at, last_run_at, "
                "next_due_at, artifact) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (record["schedule_id"], record["project_id"], record["name"], record["expression"],
                 record["mode"], record.get("target"), _json(record.get("checks") or []),
                 int(bool(record.get("enabled", True))), record.get("created_at") or stamp,
                 record.get("paused_at"), record.get("last_due_at"), record.get("last_run_at"),
                 record.get("next_due_at"), record.get("artifact")),
            )
    return get_monitoring_schedule(record["schedule_id"])  # type: ignore[return-value]


def get_monitoring_schedule(schedule_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM monitoring_schedules WHERE schedule_id=?",
                           (schedule_id,)).fetchone()
    return _monitoring_schedule_row(row) if row else None


def list_monitoring_schedules(project_id: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM monitoring_schedules"
    params: list[Any] = []
    if project_id:
        sql += " WHERE project_id=?"
        params.append(project_id)
    with connect() as conn:
        rows = conn.execute(sql + " ORDER BY project_id, created_at, schedule_id",
                           params).fetchall()
    return [_monitoring_schedule_row(row) for row in rows]


def _monitoring_schedule_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["checks"] = json.loads(item.pop("checks") or "[]")
    item["enabled"] = bool(item.get("enabled"))
    return item


def update_monitoring_schedule(schedule_id: str, **fields: Any) -> dict[str, Any] | None:
    allowed = {"enabled", "paused_at", "last_due_at", "last_run_at", "next_due_at", "artifact"}
    updates = {key: value for key, value in fields.items() if key in allowed}
    if "enabled" in updates:
        updates["enabled"] = int(bool(updates["enabled"]))
    if updates:
        assignments = ", ".join(f"{key}=?" for key in updates)
        with connect() as conn:
            conn.execute(f"UPDATE monitoring_schedules SET {assignments} WHERE schedule_id=?",  # nosec B608
                         (*updates.values(), schedule_id))
    return get_monitoring_schedule(schedule_id)


def record_monitoring_run(record: dict[str, Any]) -> dict[str, Any]:
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO monitoring_runs (schedule_id, project_id, run_id, due_at, started_at, "
            "finished_at, status, note, evidence) VALUES (?,?,?,?,?,?,?,?,?)",
            (record["schedule_id"], record["project_id"], record.get("run_id"),
             record["due_at"], record.get("started_at"), record.get("finished_at"),
             record["status"], record.get("note"), _json(record["evidence"])
             if record.get("evidence") is not None else None),
        )
        row_id = cursor.lastrowid
    return get_monitoring_run(int(row_id))  # type: ignore[arg-type]


def get_monitoring_run(row_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM monitoring_runs WHERE id=?", (row_id,)).fetchone()
    return _monitoring_run_row(row) if row else None


def update_monitoring_run(row_id: int, **fields: Any) -> dict[str, Any] | None:
    allowed = {"run_id", "started_at", "finished_at", "status", "note", "evidence"}
    updates = {key: value for key, value in fields.items() if key in allowed}
    if "evidence" in updates:
        updates["evidence"] = (_json(updates["evidence"])
                               if updates["evidence"] is not None else None)
    if updates:
        assignments = ", ".join(f"{key}=?" for key in updates)
        with connect() as conn:
            conn.execute(f"UPDATE monitoring_runs SET {assignments} WHERE id=?",  # nosec B608
                         (*updates.values(), row_id))
    return get_monitoring_run(row_id)


def find_monitoring_run(schedule_id: str, due_at: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM monitoring_runs WHERE schedule_id=? AND due_at=? "
                           "ORDER BY id DESC LIMIT 1", (schedule_id, due_at)).fetchone()
    return _monitoring_run_row(row) if row else None


def list_monitoring_runs(project_id: str | None = None, schedule_id: str | None = None,
                         limit: int = 50) -> list[dict[str, Any]]:
    sql = "SELECT * FROM monitoring_runs WHERE 1=1"
    params: list[Any] = []
    if project_id:
        sql += " AND project_id=?"
        params.append(project_id)
    if schedule_id:
        sql += " AND schedule_id=?"
        params.append(schedule_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, int(limit)))
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_monitoring_run_row(row) for row in rows]


def _monitoring_run_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    raw = item.pop("evidence", None)
    item["evidence"] = json.loads(raw) if raw else None
    return item


def record_alert(record: dict[str, Any]) -> dict[str, Any]:
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO alerts (project_id, kind, severity, finding_id, message, channel, "
            "status, detail, created_at, delivered_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (record["project_id"], record["kind"], record.get("severity"),
             record.get("finding_id"), record["message"], record["channel"], record["status"],
             _json(record.get("detail") or {}), record.get("created_at") or _now(),
             record.get("delivered_at")),
        )
        row_id = cursor.lastrowid
    return get_alert(int(row_id))  # type: ignore[arg-type]


def get_alert(row_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM alerts WHERE id=?", (row_id,)).fetchone()
    return _alert_row(row) if row else None


def update_alert(row_id: int, **fields: Any) -> dict[str, Any] | None:
    allowed = {"status", "channel", "detail", "delivered_at"}
    updates = {key: value for key, value in fields.items() if key in allowed}
    if "detail" in updates:
        updates["detail"] = _json(updates["detail"] or {})
    if updates:
        assignments = ", ".join(f"{key}=?" for key in updates)
        with connect() as conn:
            conn.execute(f"UPDATE alerts SET {assignments} WHERE id=?",  # nosec B608
                         (*updates.values(), row_id))
    return get_alert(row_id)


def list_alerts(project_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    sql = "SELECT * FROM alerts"
    params: list[Any] = []
    if project_id:
        sql += " WHERE project_id=?"
        params.append(project_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, int(limit)))
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_alert_row(row) for row in rows]


def _alert_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["detail"] = json.loads(item.get("detail") or "{}")
    return item


def stats() -> dict[str, Any]:
    with connect() as conn:
        counts = {}
        for table in ("projects", "discovery_snapshots", "tool_health", "qa_runs",
                      "report_refs", "evidence_refs", "findings", "git_analyses",
                      "regression_tests", "qa_plans", "monitoring_schedules", "alerts"):
            # table comes from the fixed tuple above, never from input.
            row = conn.execute(f"SELECT count(*) AS n FROM sqlite_master WHERE type='table' "  # nosec B608
                               f"AND name='{table}'").fetchone()
            if not row["n"]:
                continue
            counts[table] = conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]  # nosec B608
        version = conn.execute("SELECT MAX(version) AS version FROM schema_version").fetchone()
    return {"schema_version": version["version"] if version else None, **counts}
