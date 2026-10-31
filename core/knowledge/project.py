"""Per-project historical knowledge distilled from rows KHUNKHAAR already wrote.

Every section is a projection of the project's own findings, invariants, journeys, dependency
analysis and regression history. Nothing is invented, and nothing that was never recorded can
appear: a claim that has no finding row stays absent from the knowledge file. Request bodies,
credentials and file contents are never copied here; source references are repository paths only.

The document is written under `intelligence/project-knowledge/<project_id>/` as a 0600 JSON
artifact (the machine-readable source) and a Markdown digest for reading.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from typing import Any

from core.findings import store as findings_store
from core.impact import risk as impact_risk
from core.intelligence import rules as rule_classifier
from core.metadata import store
from core.shared import evidence, paths
from core.shared.redaction import redact_obj

SCHEMA = 1
MAX_ITEMS = 200
_SEVERITY_WEIGHT = {"critical": 4, "high": 3, "medium": 2, "low": 1}
_ACTIVE_STATUSES = ("confirmed", "likely", "needs_human")
_EDGE_KINDS = ("validation", "timezone", "concurrency", "atomicity", "idempotency")
_BEHAVIOUR_KINDS = ("validation", "workflow", "idempotency", "timezone", "other")
_REJECTED = ("false_positive", "not_reproducible")


def item_id(project_id: str) -> str:
    return "PK-" + hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:12]


def build(project_id: str) -> dict[str, Any]:
    """Return the full knowledge document for one project, without writing anything."""
    project = store.get_project(project_id)
    if project is None:
        raise KeyError(project_id)
    findings = findings_store.list_findings(project_id)
    invariants = findings_store.list_invariants(project_id)
    journeys = findings_store.list_journeys(project_id)
    tests = store.list_regression_tests(project_id)
    runs = store.list_regression_runs(project_id, limit=500)
    app_map = (findings_store.latest_app_map(project_id) or {}).get("summary") or {}
    impact_report = store.latest_impact_report(project_id)
    latest_run = _latest_run_by_test(runs)
    finding_by_id = {str(item["id"]): item for item in findings}
    tests_by_finding: dict[str, list[dict[str, Any]]] = {}
    for test in tests:
        tests_by_finding.setdefault(str(test.get("finding_id")), []).append(test)

    document = {
        "schema": SCHEMA,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "scope": "project",
        "project": {"project_id": project_id, "name": project.get("name"),
                    "type": project.get("project_type")},
        "counts": {
            "findings": findings_store.findings_stats(project_id),
            "invariants": len(invariants),
            "journeys": len(journeys),
            "regression_tests": len(tests),
            "regression_runs": len(runs),
        },
        "confirmed_bugs": _confirmed(findings, tests_by_finding, latest_run),
        "resolved_bugs": _resolved(findings, tests_by_finding, latest_run),
        "recurring_patterns": _recurring(findings),
        "invariants": [_invariant(item, tests, finding_by_id) for item in invariants][:MAX_ITEMS],
        "workflows": [_journey(item) for item in journeys][:MAX_ITEMS],
        "api_behaviours": _api_behaviours(invariants, findings)[:MAX_ITEMS],
        "permission_boundaries": _permission_boundaries(invariants, findings)[:MAX_ITEMS],
        "db_relations": _db_relations(app_map, invariants),
        "risky_modules": _risky_modules(findings, impact_report),
        "edge_cases": _edge_cases(invariants, findings)[:MAX_ITEMS],
        "false_positive_patterns": _rejected_claims(findings),
        "regression": _regression(tests, runs, latest_run),
        "notes": [
            "Every section is derived from this project's own finding, invariant, journey, "
            "analysis and regression rows; a claim with no row is absent.",
            "Request and response bodies, credentials and file contents are not copied into "
            "knowledge. Source references are repository paths, not absolute paths.",
            "A passing regression run means the recorded defect was not observed in that "
            "environment; it is not proof that a code fix landed.",
            "The impact score copied from the latest report is an approximate prioritisation "
            "signal, not a probability of failure.",
            "This document is scoped to one project and stored under the KHUNKHAAR root.",
        ],
    }
    return redact_obj(document)


def save(project_id: str) -> dict[str, Any]:
    document = build(project_id)
    folder = paths.project_knowledge_dir(project_id)
    json_path = evidence.write_json(folder, "knowledge.json", document,
                                    project_id=project_id, kind="project-knowledge")
    markdown = _markdown(document)
    md_path = evidence.write_bytes(folder, "knowledge.md", markdown.encode("utf-8"),
                                   project_id=project_id, kind="project-knowledge")
    item = store.upsert_knowledge_item({
        "item_id": item_id(project_id),
        "scope": "project",
        "project_id": project_id,
        "kind": "project-knowledge",
        "title": f"Project knowledge: {document['project']['name'] or project_id}",
        "body_path": evidence.relative(json_path),
        "tags": ["project", "knowledge", document["project"]["type"] or "unknown"],
    })
    return {"project_id": project_id, "document": document, "json": evidence.relative(json_path),
            "markdown": evidence.relative(md_path), "item": item}


def _finding_summary(finding: dict[str, Any]) -> dict[str, Any]:
    return {
        "finding_id": finding.get("id"),
        "title": finding.get("title"),
        "feature": finding.get("feature"),
        "category": finding.get("category"),
        "severity": finding.get("severity"),
        "status": finding.get("status"),
        "rule_ids": finding.get("rule_ids") or [],
        "source_refs": [impact_risk.ref_path(ref) for ref in finding.get("source_refs") or []],
        "expected": finding.get("expected"),
        "observed": finding.get("actual"),
        "remediation": finding.get("remediation"),
    }


def _confirmed(findings: list[dict[str, Any]], tests_by_finding: dict[str, list[dict[str, Any]]],
               latest_run: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for finding in findings:
        if finding.get("status") != "confirmed":
            continue
        row = _finding_summary(finding)
        row["regression_tests"] = [
            _test_ref(test, latest_run.get(str(test["test_id"])))
            for test in tests_by_finding.get(str(finding["id"]), [])
        ]
        rows.append(row)
    rows.sort(key=lambda row: (-_SEVERITY_WEIGHT.get(str(row.get("severity")), 0),
                               str(row.get("finding_id"))))
    return rows[:MAX_ITEMS]


def _resolved(findings: list[dict[str, Any]], tests_by_finding: dict[str, list[dict[str, Any]]],
              latest_run: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for finding in findings:
        if finding.get("status") != "confirmed":
            continue
        for test in tests_by_finding.get(str(finding["id"]), []):
            run = latest_run.get(str(test["test_id"])) or {}
            if run.get("result") != "pass":
                continue
            rows.append({
                "finding_id": finding.get("id"),
                "title": finding.get("title"),
                "test_id": test.get("test_id"),
                "reliability": test.get("reliability"),
                "last_success_at": test.get("last_success_at"),
                "evidence": run.get("evidence"),
                "note": "the defect was not observed in this run's environment; this is not "
                        "proof of a fix unless the environment matched the affected one",
            })
    return rows[:MAX_ITEMS]


def _recurring(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for finding in findings:
        if finding.get("status") not in ("confirmed", "likely"):
            continue
        groups.setdefault(str(finding.get("category") or "other"), []).append(finding)
    rows = []
    for category, items in groups.items():
        if len(items) < 2:
            continue
        features = sorted({str(item.get("feature")) for item in items if item.get("feature")})
        rows.append({
            "pattern": f"{category} defects recur across {len(items)} findings",
            "category": category,
            "count": len(items),
            "features": features[:12],
            "finding_ids": [item.get("id") for item in items][:MAX_ITEMS],
            "note": "a repeated category is a signal to examine the shared code path, not a "
                    "proven common cause",
        })
    rows.sort(key=lambda row: (-row["count"], row["category"]))
    return rows


def _invariant(invariant: dict[str, Any], tests: list[dict[str, Any]],
               finding_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    coverage = []
    for test in tests:
        finding = finding_by_id.get(str(test.get("finding_id"))) or {}
        if invariant.get("rule_id") and invariant["rule_id"] in (finding.get("rule_ids") or []):
            coverage.append(test.get("test_id"))
    return {
        "invariant_id": invariant.get("id"),
        "rule_id": invariant.get("rule_id"),
        "description": invariant.get("description"),
        "feature": invariant.get("feature"),
        "kind": _kind_of(invariant),
        "verification": invariant.get("method"),
        "confidence": invariant.get("confidence"),
        "source": invariant.get("source"),
        "regression_coverage": coverage,
    }


def _journey(journey: dict[str, Any]) -> dict[str, Any]:
    steps = []
    for step in journey.get("steps") or []:
        if not isinstance(step, dict):
            continue
        steps.append({key: step.get(key) for key in ("name", "method", "path", "actor", "expect")})
    return {"journey_id": journey.get("id"), "name": journey.get("name"),
            "steps": steps, "created_at": journey.get("created_at")}


def _api_behaviours(invariants: list[dict[str, Any]],
                    findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for invariant in invariants:
        if _kind_of(invariant) not in _BEHAVIOUR_KINDS:
            continue
        rows.append({
            "source": "invariant",
            "reference": invariant.get("rule_id") or invariant.get("id"),
            "feature": invariant.get("feature"),
            "behaviour": invariant.get("description"),
            "verification": invariant.get("method"),
        })
    for finding in findings:
        if finding.get("status") != "confirmed":
            continue
        if finding.get("category") not in ("api", "functional", "workflow", "business_logic",
                                           "reliability"):
            continue
        rows.append({
            "source": "finding",
            "reference": finding.get("id"),
            "feature": finding.get("feature"),
            "behaviour": finding.get("expected"),
            "observed": finding.get("actual"),
        })
    return rows


def _permission_boundaries(invariants: list[dict[str, Any]],
                           findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for invariant in invariants:
        if _kind_of(invariant) != "authorization":
            continue
        rows.append({
            "source": "invariant",
            "reference": invariant.get("rule_id") or invariant.get("id"),
            "feature": invariant.get("feature"),
            "boundary": invariant.get("description"),
            "roles": invariant.get("roles") or [],
        })
    for finding in findings:
        if finding.get("status") != "confirmed" or finding.get("category") != "security":
            continue
        rows.append({
            "source": "finding",
            "reference": finding.get("id"),
            "feature": finding.get("feature"),
            "boundary": finding.get("expected"),
            "role": finding.get("role"),
        })
    return rows


def _db_relations(app_map: dict[str, Any],
                  invariants: list[dict[str, Any]]) -> dict[str, Any]:
    entities = [{"name": item.get("name"), "source": item.get("source"), "line": item.get("line")}
                for item in (app_map.get("entities") or []) if isinstance(item, dict)]
    links = []
    for invariant in invariants:
        names = invariant.get("entities") or []
        if not names:
            continue
        links.append({"reference": invariant.get("rule_id") or invariant.get("id"),
                      "feature": invariant.get("feature"), "entities": names[:12]})
    return {
        "entities": entities[:MAX_ITEMS],
        "invariant_links": links[:MAX_ITEMS],
        "note": ("entities are static model declarations, not a live database schema; relation "
                 "links show which invariants name an entity") if entities else
                "no application map with data entities has been recorded for this project",
    }


def _risky_modules(findings: list[dict[str, Any]],
                   impact_report: dict[str, Any] | None) -> dict[str, Any]:
    features: dict[str, dict[str, Any]] = {}
    directories: dict[str, int] = {}
    for finding in findings:
        if finding.get("status") not in _ACTIVE_STATUSES:
            continue
        feature = str(finding.get("feature") or "application")
        entry = features.setdefault(feature, {"feature": feature, "findings": 0, "by_severity": {}})
        entry["findings"] += 1
        severity = str(finding.get("severity") or "medium")
        entry["by_severity"][severity] = entry["by_severity"].get(severity, 0) + 1
        for ref in finding.get("source_refs") or []:
            path = impact_risk.ref_path(ref)
            head = path.split("/", 1)[0] if "/" in path else path
            if head:
                directories[head] = directories.get(head, 0) + 1
    ranked = sorted(features.values(),
                    key=lambda item: (-sum(_SEVERITY_WEIGHT.get(name, 0) * count
                                           for name, count in item["by_severity"].items()),
                                      item["feature"]))
    impact = None
    if impact_report:
        impact = {
            "report_id": impact_report.get("report_id"),
            "score": impact_report.get("risk_score"),
            "band_wording": "approximate prioritisation signal, not a probability",
            "created_at": impact_report.get("created_at"),
        }
    return {
        "features": ranked[:MAX_ITEMS],
        "top_directories": [{"directory": name, "findings": count}
                            for name, count in sorted(directories.items(),
                                                      key=lambda pair: (-pair[1], pair[0]))[:12]],
        "latest_impact": impact,
        "note": "feature counts include confirmed, likely and needs_human findings; severities "
                "weight ordering, they are not a failure probability",
    }


def _edge_cases(invariants: list[dict[str, Any]],
                findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for invariant in invariants:
        kind = _kind_of(invariant)
        if kind not in _EDGE_KINDS:
            continue
        rows.append({"source": "invariant", "reference": invariant.get("rule_id")
                     or invariant.get("id"), "feature": invariant.get("feature"),
                     "case": invariant.get("description"), "kind": kind})
    for finding in findings:
        if finding.get("status") != "confirmed":
            continue
        if finding.get("category") not in _EDGE_KINDS:
            continue
        rows.append({"source": "finding", "reference": finding.get("id"),
                     "feature": finding.get("feature"), "case": finding.get("title"),
                     "kind": finding.get("category")})
    return rows


def _rejected_claims(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for finding in findings:
        status = str(finding.get("status") or "")
        if status not in _REJECTED:
            continue
        events = findings_store.list_events(str(finding["id"]))
        event = next((item for item in reversed(events)
                      if item.get("action") == f"status:{status}"), {})
        rows.append({
            "finding_id": finding.get("id"),
            "title": finding.get("title"),
            "feature": finding.get("feature"),
            "category": finding.get("category"),
            "status": status,
            "reason": event.get("note") or "no reason was recorded with the status change",
            "rule_ids": finding.get("rule_ids") or [],
        })
    return rows[:MAX_ITEMS]


def _regression(tests: list[dict[str, Any]], runs: list[dict[str, Any]],
                latest_run: dict[str, dict[str, Any]]) -> dict[str, Any]:
    by_reliability: dict[str, int] = {}
    for test in tests:
        key = str(test.get("reliability") or "generated")
        by_reliability[key] = by_reliability.get(key, 0) + 1
    by_result: dict[str, int] = {}
    for run in runs:
        key = str(run.get("result") or "unknown")
        by_result[key] = by_result.get(key, 0) + 1
    green = [run for run in runs if run.get("result") == "pass"]
    return {
        "tests": len(tests),
        "by_reliability": by_reliability,
        "runs": len(runs),
        "by_result": by_result,
        "last_success_at": max((run.get("finished_at") or "" for run in green), default=None),
        "latest": [_test_ref(test, latest_run.get(str(test["test_id"]))) for test in tests][:MAX_ITEMS],
        "note": "environment_unavailable and not_run are environment results, never evidence "
                "about the application",
    }


def _test_ref(test: dict[str, Any], run: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "test_id": test.get("test_id"),
        "finding_id": test.get("finding_id"),
        "reliability": test.get("reliability"),
        "method": test.get("method"),
        "last_success_at": test.get("last_success_at"),
        "last_failure_at": test.get("last_failure_at"),
        "latest_run": None if run is None else {
            "result": run.get("result"), "finished_at": run.get("finished_at"),
            "run_id": run.get("run_id"), "evidence": run.get("evidence"),
        },
    }


def _kind_of(invariant: dict[str, Any]) -> str:
    """The invariant table stores how to verify a rule but not its kind; classify the text again.

    The classifier is the same one that produced the original rule kind, so this is a
    deterministic re-read of the recorded description, not a new judgement.
    """
    return rule_classifier.classify(str(invariant.get("description") or ""))


def _latest_run_by_test(runs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for run in runs:
        latest.setdefault(str(run.get("test_id")), run)
    return latest


def _markdown(document: dict[str, Any]) -> str:
    project = document["project"]
    lines = [
        f"# Project knowledge: {project.get('name') or project.get('project_id')}",
        "",
        f"Generated {document['generated_at']} from this project's own KHUNKHAAR records.",
        "",
        "## Confirmed bugs",
        "",
    ]
    _bullets(lines, document["confirmed_bugs"],
             lambda row: f"{row['finding_id']} [{row.get('severity')}] {row.get('title')} "
                         f"({row.get('feature')} / {row.get('category')})")
    lines += ["", "## Resolved or not reproducing", ""]
    _bullets(lines, document["resolved_bugs"],
             lambda row: f"{row['finding_id']} - test {row['test_id']} last passed "
                         f"{row.get('last_success_at')}")
    lines += ["", "## Recurring patterns", ""]
    _bullets(lines, document["recurring_patterns"],
             lambda row: f"{row['pattern']} (features: {', '.join(row['features']) or '-'})")
    lines += ["", "## Invariants", ""]
    _bullets(lines, document["invariants"],
             lambda row: f"{row.get('rule_id') or row.get('invariant_id')}: "
                         f"{row.get('description')} ({row.get('verification')})")
    lines += ["", "## Workflows", ""]
    _bullets(lines, document["workflows"],
             lambda row: f"{row.get('name')}: "
                         + " -> ".join(f"{step.get('method')} {step.get('path')}"
                                       for step in row.get("steps") or []))
    lines += ["", "## API behaviours", ""]
    _bullets(lines, document["api_behaviours"],
             lambda row: f"{row.get('feature')}: {row.get('behaviour')}")
    lines += ["", "## Permission boundaries", ""]
    _bullets(lines, document["permission_boundaries"],
             lambda row: f"{row.get('feature')}: {row.get('boundary')}")
    lines += ["", "## Database entities", ""]
    entities = document["db_relations"].get("entities") or []
    if entities:
        lines += [f"- {item.get('name')} ({item.get('source')}:{item.get('line')})"
                  for item in entities]
    else:
        lines.append(f"- {document['db_relations'].get('note')}")
    lines += ["", "## Risky modules", ""]
    _bullets(lines, document["risky_modules"]["features"],
             lambda row: f"{row['feature']}: {row['findings']} open finding(s) "
                         f"{row['by_severity']}")
    lines += ["", "## Edge cases", ""]
    _bullets(lines, document["edge_cases"], lambda row: f"{row.get('feature')}: {row.get('case')}")
    lines += ["", "## Rejected claims and false-positive patterns", ""]
    _bullets(lines, document["false_positive_patterns"],
             lambda row: f"{row['finding_id']} {row['status']}: {row['reason']}")
    lines += ["", "## Regression history", "",
              f"- tests: {document['regression']['tests']} "
              f"{document['regression']['by_reliability']}",
              f"- runs: {document['regression']['runs']} {document['regression']['by_result']}",
              f"- last success: {document['regression']['last_success_at'] or '-'}",
              "", "## Notes", ""]
    lines += [f"- {note}" for note in document.get("notes") or []]
    return "\n".join(lines) + "\n"


def _bullets(lines: list[str], rows: list[dict[str, Any]], render) -> None:
    if not rows:
        lines.append("- none recorded")
        return
    for row in rows:
        lines.append(f"- {render(row)}")
