"""Read-only data assembly for the dashboard.

Every function here reads the metadata store, the findings store and the evidence/report index.
None of them starts a process, sends a request, touches a repository or writes a file. Free-text
fields are redacted and length-capped before they reach a template, and evidence paths must resolve
inside the platform's own `evidence/` or `reports/` trees.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from core.findings import store as findings_store
from core.impact.risk import WORDING as RISK_WORDING
from core.metadata import store
from core.shared import paths
from core.shared.redaction import redact_text

_OPEN_STATUSES = ("unverified", "likely", "needs_human")
_RESOLVED_STATUSES = ("not_reproducible", "false_positive")
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})
_TEXT_SUFFIXES = frozenset({".json", ".txt", ".log", ".md", ".yaml", ".yml", ".csv", ".har",
                            ".xml", ".html"})
_MAX_JSON_BYTES = 4 * 1024 * 1024
_MAX_TEXT_BYTES = 256 * 1024


def clean(value: Any, limit: int = 300) -> str:
    """Redact and cap a value that came from the store or a project artifact."""
    text = redact_text(str(value or ""))
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def _integer(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _rate(part: int, whole: int) -> float | None:
    return round(100.0 * part / whole, 1) if whole else None


def risk_band(score: float) -> str:
    if score >= 70:
        return "high attention"
    if score >= 45:
        return "elevated attention"
    if score >= 20:
        return "moderate attention"
    return "low attention"


def _allowed_roots() -> tuple[Path, Path]:
    root = paths.kh_root().resolve()
    return (root / "evidence", root / "reports")


def resolve_artifact(relative: str | None) -> Path | None:
    """A stored artifact path resolved inside evidence/ or reports/, or None."""
    if not relative or Path(str(relative)).is_absolute():
        return None
    try:
        candidate = (paths.kh_root() / str(relative)).resolve()
    except (OSError, ValueError):
        return None
    if not any(candidate.is_relative_to(base) for base in _allowed_roots()):
        return None
    return candidate if candidate.is_file() else None


def _read_json(relative: str | None) -> dict[str, Any] | None:
    target = resolve_artifact(relative)
    if target is None or target.stat().st_size > _MAX_JSON_BYTES:
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def read_text_evidence(relative: str) -> tuple[str, bool] | None:
    """Redacted text for the evidence route, or None when the path is outside the safe roots."""
    target = resolve_artifact(relative)
    if target is None:
        return None
    suffix = target.suffix.lower()
    if suffix not in _TEXT_SUFFIXES:
        return None
    try:
        with target.open("rb") as handle:
            raw = handle.read(_MAX_TEXT_BYTES + 1)
        truncated = len(raw) > _MAX_TEXT_BYTES
    except OSError:
        return None
    text = raw[:_MAX_TEXT_BYTES].decode("utf-8", errors="replace")
    return redact_text(text), truncated


def is_image(relative: str) -> bool:
    return Path(relative).suffix.lower() in _IMAGE_SUFFIXES


def _project(row: dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    for key in ("languages", "frameworks"):
        try:
            item[key] = json.loads(item.get(key) or "[]")
        except (TypeError, ValueError):
            item[key] = []
    item["name"] = clean(item.get("name"), 120)
    return item


def _run(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": clean(row.get("run_id"), 80),
        "project_id": row.get("project_id"),
        "kind": clean(row.get("kind"), 80),
        "status": clean(row.get("status"), 40),
        "started_at": row.get("started_at"),
        "finished_at": row.get("finished_at"),
        "note": clean(row.get("note"), 200),
    }


def _finding(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "title": clean(row.get("title"), 220),
        "description": clean(row.get("description"), 500),
        "feature": clean(row.get("feature"), 80),
        "category": clean(row.get("category"), 60),
        "severity": clean(row.get("severity"), 20),
        "status": clean(row.get("status"), 30),
        "confidence": round(float(row.get("confidence") or 0.0), 2),
        "updated_at": row.get("updated_at"),
    }


def _file_change(change: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": clean(change.get("path"), 300),
        "old_path": clean(change.get("old_path"), 300),
        "status": clean(change.get("status"), 30),
        "insertions": _integer(change.get("insertions")),
        "deletions": _integer(change.get("deletions")),
        "binary": bool(change.get("binary")),
        "functions": [clean(name, 120) for name in (change.get("functions") or [])][:20],
        "attribution": clean(change.get("attribution"), 30),
        "hunks": _integer(change.get("hunks")),
    }


def _impact_summary(row: dict[str, Any] | None, artifact: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    details = (artifact or {}).get("impact") or {}
    risk = (artifact or {}).get("risk") or {}
    score = float(row.get("risk_score") or 0.0)
    return {
        "report_id": row.get("report_id"),
        "created_at": row.get("created_at"),
        "base_ref": clean(row.get("base_ref"), 80),
        "head_ref": clean(row.get("head_ref"), 80),
        "score": round(score, 2),
        "band": risk_band(score),
        "wording": clean(RISK_WORDING, 240),
        "factors": [
            {"name": clean(item.get("name"), 80),
             "weight": round(float(item.get("weight") or 0.0), 2),
             "contribution": round(float(item.get("contribution") or 0.0), 2)}
            for item in (risk.get("factors") or row.get("risk_factors") or [])
            if isinstance(item, dict)
        ],
        "impacted": [
            {"type": clean(item.get("type"), 30), "name": clean(item.get("name"), 200),
             "path": clean(item.get("path"), 300), "via": clean(item.get("via"), 200),
             "depth": _integer(item.get("depth"))}
            for item in (details.get("impacted") or [])[:100]
            if isinstance(item, dict)
        ],
        "surface": [
            {"type": clean(item.get("type"), 30), "name": clean(item.get("name"), 200),
             "via": clean(item.get("via"), 200)}
            for item in (details.get("surface") or [])[:100]
            if isinstance(item, dict)
        ],
        "tests": [
            {"name": clean(item.get("name"), 200), "path": clean(item.get("path"), 300)}
            for item in (details.get("tests") or [])[:100]
            if isinstance(item, dict)
        ],
        "workflows": [
            {"name": clean(item.get("name"), 200), "feature": clean(item.get("feature"), 120)}
            for item in (details.get("workflows") or [])[:100]
            if isinstance(item, dict)
        ],
        "summary": {
            "impacted": _integer((details.get("summary") or {}).get("impacted")),
            "surface": _integer((details.get("summary") or {}).get("surface")),
            "depth": _integer((details.get("summary") or {}).get("depth")),
        },
    }


def _latest_report(run_id: str | None) -> tuple[dict[str, Any], str | None]:
    if not run_id:
        return {}, None
    for ref in store.list_report_refs(run_id=run_id, limit=10):
        payload = _read_json(ref.get("path"))
        if payload is not None:
            return payload, ref.get("path")
    return {}, None


def _agents(report: dict[str, Any]) -> list[dict[str, Any]]:
    raw = report.get("agents")
    if not isinstance(raw, dict):
        return []
    rows = []
    for name, info in raw.items():
        if not isinstance(info, dict):
            continue
        rows.append({
            "agent": clean(name, 80),
            "missions": _integer(info.get("missions")),
            "issues": _integer(info.get("issues")),
            "infrastructure_errors": _integer(info.get("infrastructure_errors")),
        })
    return sorted(rows, key=lambda item: item["agent"])


def _mission_counts(report: dict[str, Any]) -> dict[str, Any] | None:
    raw = report.get("missions")
    if not isinstance(raw, dict):
        return None
    return {
        "planned": _integer(raw.get("planned")),
        "executed": _integer(raw.get("executed")),
        "skipped": _integer(raw.get("skipped")),
        "failed": _integer(raw.get("failed")),
    }


def _verification(report: dict[str, Any], evidence_by_path: dict[str, int]) -> list[dict[str, Any]]:
    rows = []
    for item in report.get("verification") or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("evidence") or "")
        rows.append({
            "finding_id": clean(item.get("finding_id"), 40),
            "status": clean(item.get("status"), 30),
            "attempts": _integer(item.get("attempts")),
            "reproductions": _integer(item.get("reproductions")),
            "evidence": clean(path, 300),
            "evidence_id": evidence_by_path.get(path),
        })
    return rows


def _profile(project: dict[str, Any]) -> dict[str, Any]:
    """The discovery profile, read only from the registry tree."""
    raw_path = project.get("profile_path")
    if not raw_path:
        return {}
    try:
        candidate = Path(str(raw_path)).resolve()
        candidate.relative_to(paths.registry_dir().resolve())
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _project_findings(project_id: str) -> list[dict[str, Any]]:
    return [_finding(row) for row in findings_store.list_findings(project_id)]


def _regression_tests(project_id: str) -> list[dict[str, Any]]:
    rows = []
    for row in store.list_regression_tests(project_id):
        rows.append({
            "test_id": clean(row.get("test_id"), 60),
            "finding_id": clean(row.get("finding_id"), 40),
            "objective": clean(row.get("objective"), 300),
            "category": clean(row.get("category"), 60),
            "method": clean(row.get("method"), 40),
            "reliability": clean(row.get("reliability"), 30),
            "confidence": round(float(row.get("confidence") or 0.0), 2),
            "last_success_at": row.get("last_success_at"),
            "last_failure_at": row.get("last_failure_at"),
        })
    return rows


def _regression_runs(project_id: str, limit: int = 20) -> list[dict[str, Any]]:
    return [
        {"test_id": clean(row.get("test_id"), 60), "result": clean(row.get("result"), 30),
         "finished_at": row.get("finished_at"), "note": clean(row.get("note"), 200),
         "duration_ms": _integer(row.get("duration_ms"))}
        for row in store.list_regression_runs(project_id, limit=limit)
    ]


def _latest_analysis(project_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    row = store.latest_git_analysis(project_id)
    artifact = _read_json(row.get("artifact")) if row else None
    return row, ((artifact or {}).get("changeset") if isinstance(artifact, dict) else None)


def _changeset(project_id: str, limit: int = 150) -> dict[str, Any] | None:
    _, changeset = _latest_analysis(project_id)
    if not changeset:
        return None
    files = [_file_change(item) for item in (changeset.get("files") or [])[:limit]
             if isinstance(item, dict)]
    return {
        "kind": clean(changeset.get("kind"), 40),
        "base": clean(changeset.get("base"), 80),
        "head": clean(changeset.get("head"), 80),
        "base_source": clean(changeset.get("base_source"), 40),
        "summary": changeset.get("summary") or {},
        "files": files,
        "total_files": len(changeset.get("files") or []),
        "truncated": bool(changeset.get("truncated")),
        "notes": [clean(note, 240) for note in (changeset.get("notes") or [])][:10],
    }


def overview() -> dict[str, Any]:
    projects = [_project(row) for row in store.list_projects()]
    runs = store.list_qa_runs(limit=500)
    runs_by_project: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        runs_by_project.setdefault(run.get("project_id"), []).append(run)
    names = {project["project_id"]: project["name"] for project in projects}

    cards = []
    agents: dict[str, dict[str, Any]] = {}
    totals = {"projects": len(projects), "executions": len(runs), "confirmed": 0, "open": 0,
              "not_reproduced": 0, "false_positive": 0, "security": 0,
              "regression_pass": 0, "regression_fail": 0, "regression_unavailable": 0}
    for project in projects:
        project_id = project["project_id"]
        stats = findings_store.findings_stats(project_id)
        by_status = stats.get("by_status") or {}
        regression = store.regression_stats(project_id)
        impacts = store.list_impact_reports(project_id, limit=8)
        latest_impact = impacts[0] if impacts else None
        project_runs = runs_by_project.get(project_id, [])
        latest_run = project_runs[0] if project_runs else None
        report, _ = _latest_report(latest_run.get("run_id") if latest_run else None)
        project_agents = _agents(report)
        for agent in project_agents:
            row = agents.setdefault(agent["agent"], {"agent": agent["agent"], "missions": 0,
                                                     "issues": 0, "infrastructure_errors": 0,
                                                     "projects": 0})
            row["missions"] += agent["missions"]
            row["issues"] += agent["issues"]
            row["infrastructure_errors"] += agent["infrastructure_errors"]
            row["projects"] += 1
        confirmed = _integer(by_status.get("confirmed"))
        open_count = sum(_integer(by_status.get(status)) for status in _OPEN_STATUSES)
        fp = _integer(by_status.get("false_positive"))
        not_reproduced = _integer(by_status.get("not_reproducible"))
        card = {
            "project": project,
            "runs": len(project_runs),
            "latest_run": _run(latest_run) if latest_run else None,
            "findings": stats,
            "confirmed": confirmed,
            "open": open_count,
            "not_reproduced": not_reproduced,
            "false_positive": fp,
            "false_positive_rate": _rate(fp, confirmed + fp),
            "security": _integer((stats.get("by_category") or {}).get("security")),
            "regression": regression,
            "agents": project_agents,
            "latest_impact": None if latest_impact is None else {
                "score": round(float(latest_impact.get("risk_score") or 0.0), 2),
                "band": risk_band(float(latest_impact.get("risk_score") or 0.0)),
                "created_at": latest_impact.get("created_at"),
            },
            "risk_points": [
                {"score": round(float(item.get("risk_score") or 0.0), 2),
                 "band": risk_band(float(item.get("risk_score") or 0.0)),
                 "created_at": item.get("created_at")}
                for item in impacts
            ],
        }
        cards.append(card)
        totals["confirmed"] += confirmed
        totals["open"] += open_count
        totals["not_reproduced"] += not_reproduced
        totals["false_positive"] += fp
        totals["security"] += card["security"]
        totals["regression_pass"] += _integer((regression.get("by_result") or {}).get("pass"))
        totals["regression_fail"] += _integer((regression.get("by_result") or {}).get("fail"))
        totals["regression_unavailable"] += _integer(
            (regression.get("by_result") or {}).get("environment_unavailable"))

    totals["false_positive_rate"] = _rate(totals["false_positive"],
                                          totals["confirmed"] + totals["false_positive"])
    recent = []
    for run in runs[:12]:
        item = _run(run)
        item["project_name"] = names.get(run.get("project_id"), "unregistered")
        recent.append(item)
    return {
        "cards": cards,
        "totals": totals,
        "agents": sorted(agents.values(), key=lambda item: item["agent"]),
        "recent_runs": recent,
        "risk_wording": RISK_WORDING,
    }


def project_detail(project_id: str) -> dict[str, Any] | None:
    row = store.get_project(project_id)
    if not row:
        return None
    project = _project(row)
    stats = findings_store.findings_stats(project_id)
    regression = store.regression_stats(project_id)
    graph = store.load_impact_graph(project_id)
    nodes = {node["node_id"]: node for node in graph.get("nodes") or []}
    incoming = Counter(edge.get("dst") for edge in graph.get("edges") or [])
    risky = []
    for node_id, count in incoming.most_common(12):
        node = nodes.get(node_id)
        if not node:
            continue
        risky.append({
            "type": clean(node.get("type"), 30),
            "name": clean(node.get("name"), 200),
            "path": clean(node.get("path"), 300),
            "referrers": count,
        })
    profile = _profile(project)
    impact_rows = store.list_impact_reports(project_id, limit=8)
    imp_row = impact_rows[0] if impact_rows else None
    impact = _impact_summary(imp_row, _read_json(imp_row.get("artifact")) if imp_row else None)
    return {
        "project": project,
        "profile": {
            "git": profile.get("git") or {},
            "detected_at": profile.get("detected_at"),
            "notes": [clean(note, 200) for note in (profile.get("notes") or [])][:10],
        },
        "findings": _project_findings(project_id),
        "stats": stats,
        "regression": regression,
        "regression_tests": _regression_tests(project_id),
        "regression_runs": _regression_runs(project_id),
        "invariants": [
            {"rule_id": clean(item.get("rule_id"), 40),
             "description": clean(item.get("description"), 500),
             "feature": clean(item.get("feature"), 120),
             "method": clean(item.get("method"), 60),
             "roles": [clean(role, 40) for role in (item.get("roles") or [])],
             "confidence": round(float(item.get("confidence") or 0.0), 2),
             "source": clean(item.get("source"), 200)}
            for item in findings_store.list_invariants(project_id)
        ],
        "graph": {"info": store.impact_graph_info(project_id), "risky": risky,
                  "wording": ("Most-depended-on nodes are a heuristic, not a defect count.")},
        "impact": impact,
        "impact_trend": [
            {"score": round(float(item.get("risk_score") or 0.0), 2),
             "band": risk_band(float(item.get("risk_score") or 0.0)),
             "created_at": item.get("created_at")}
            for item in impact_rows
        ],
        "changeset": _changeset(project_id),
        "runs": [_run(run) for run in store.list_qa_runs(project_id, limit=15)],
        "risk_wording": RISK_WORDING,
    }


def execution_detail(run_id: str) -> dict[str, Any] | None:
    run = store.get_run(run_id)
    if not run:
        return None
    project = store.get_project(run.get("project_id")) if run.get("project_id") else None
    evidence = [
        {"id": item.get("id"), "kind": clean(item.get("kind"), 40),
         "path": clean(item.get("path"), 300),
         "retention_class": clean(item.get("retention_class"), 40),
         "bytes": _integer(item.get("bytes")),
         "image": is_image(str(item.get("path") or ""))}
        for item in store.list_evidence_refs(run_id=run_id, limit=500)
    ]
    evidence_by_path = {item["path"]: item["id"] for item in evidence}
    report, report_path = _latest_report(run_id)
    findings = _project_findings(run["project_id"]) if run.get("project_id") else []
    report_refs = [{"path": clean(item.get("path"), 300), "created_at": item.get("created_at")}
                   for item in store.list_report_refs(run_id=run_id, limit=50)]
    return {
        "run": _run(run),
        "project": _project(project) if project else None,
        "report_path": clean(report_path, 300),
        "report_refs": report_refs,
        "agents": _agents(report),
        "missions": _mission_counts(report),
        "budget": (report.get("budget") if isinstance(report.get("budget"), dict) else None),
        "errors_recorded": len(report.get("errors") or []),
        "verification": _verification(report, evidence_by_path),
        "findings": findings,
        "evidence": evidence,
        "screenshots": [item for item in evidence if item["image"]][:24],
        "logs": [item for item in evidence if item["path"].endswith(".log")
                 or item["kind"] in ("log", "text")][:24],
        "traces": [item for item in evidence
                   if item["path"].endswith(".zip") or ".trace" in item["path"]][:24],
        "tools": [
            {"tool": clean(item.get("tool"), 60), "status": clean(item.get("status"), 30),
             "version": clean(item.get("version"), 60)}
            for item in store.tool_health()
        ],
    }


def diff_detail(project_id: str) -> dict[str, Any] | None:
    row = store.get_project(project_id)
    if not row:
        return None
    project = _project(row)
    analysis_row, _ = _latest_analysis(project_id)
    imp_row = store.latest_impact_report(project_id)
    impact = _impact_summary(imp_row, _read_json(imp_row.get("artifact")) if imp_row else None)
    plans = store.list_qa_plans(project_id, limit=1)
    plan = store.get_qa_plan(plans[0]["plan_id"]) if plans else None
    plan_actions = []
    if plan:
        plan_actions = [
            {"agent": clean(action.get("agent"), 80), "kind": clean(action.get("kind"), 60),
             "feature": clean(action.get("feature"), 120),
             "requires_approval": bool(action.get("requires_approval")),
             "requests": _integer(action.get("est_requests")),
             "steps": _integer(action.get("est_steps")),
             "selected_tests": len(action.get("selected_tests") or [])}
            for action in plan.get("actions") or []
        ]
    runs = store.list_qa_runs(project_id, limit=5)
    analysis = None
    if analysis_row:
        analysis = {
            "kind": clean(analysis_row.get("kind"), 40),
            "base_ref": clean(analysis_row.get("base_ref"), 80),
            "head_ref": clean(analysis_row.get("head_ref"), 80),
            "created_at": analysis_row.get("created_at"),
            "commits": _integer(analysis_row.get("commits")),
            "files_changed": _integer(analysis_row.get("files_changed")),
            "insertions": _integer(analysis_row.get("insertions")),
            "deletions": _integer(analysis_row.get("deletions")),
            "renames": _integer(analysis_row.get("renames")),
            "removed": _integer(analysis_row.get("removed")),
            "conflicted": bool(analysis_row.get("conflicted")),
            "worktree_clean": bool(analysis_row.get("worktree_clean")),
            "index_untouched": bool(analysis_row.get("index_untouched")),
        }
    return {
        "project": project,
        "analysis": analysis,
        "changeset": _changeset(project_id, limit=200),
        "impact": impact,
        "plan": None if not plan else {
            "plan_id": clean(plan.get("plan_id"), 60),
            "mode": clean(plan.get("mode"), 30),
            "status": clean(plan.get("status"), 30),
            "digest": clean(plan.get("digest"), 80),
            "created_at": plan.get("created_at"),
            "approved_by": clean(plan.get("approved_by"), 80),
            "approved_at": plan.get("approved_at"),
            "actions": plan_actions,
        },
        "outcome": _run(runs[0]) if runs else None,
        "risk_wording": RISK_WORDING,
    }
