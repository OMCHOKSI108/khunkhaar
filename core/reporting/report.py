"""Reports generated from persistent state.

A report is a view over SQLite, never over in-memory run state. This is what makes it possible
to inspect a run after the process exits, and it keeps confirmed findings and unverified
suspicions visibly separate.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.findings import store as findings_store
from core.shared import evidence as ev
from core.shared import paths


def build_report(project_id: str) -> dict[str, Any]:
    findings = findings_store.list_findings(project_id)
    for finding in findings:
        finding["events"] = findings_store.list_events(finding["id"])
    return {
        "project_id": project_id,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stats": findings_store.findings_stats(project_id),
        "invariants": findings_store.list_invariants(project_id),
        "missions": findings_store.list_missions(project_id),
        "findings": findings,
    }


def render_markdown(report: dict[str, Any]) -> str:
    stats = report["stats"]
    lines = [
        f"# KHUNKHAAR QA report — {report['project_id']}",
        "",
        f"Generated: {report['generated_at']}",
        "",
        "## Summary",
        "",
        f"- Findings: {stats['total']}",
        f"- By status: {_inline(stats['by_status'])}",
        f"- By severity: {_inline(stats['by_severity'])}",
        f"- By category: {_inline(stats['by_category'])}",
        f"- Invariants extracted from the application: {len(report['invariants'])}",
        "",
        "## Findings",
        "",
        "| ID | Status | Severity | Category | Feature | Title | Reproduced |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for finding in report["findings"]:
        lines.append("| {id} | {status} | {severity} | {category} | {feature} | {title} | {rep} |".format(
            id=finding["id"], status=finding["status"], severity=finding["severity"],
            category=finding["category"], feature=finding["feature"],
            title=str(finding["title"]).replace("|", "/")[:90],
            rep=finding.get("reproducibility") or "-"))
    if not report["findings"]:
        lines.append("| - | - | - | - | - | no findings recorded | - |")

    confirmed = [f for f in report["findings"] if f["status"] == "confirmed"]
    lines += ["", "## Confirmed findings (independently reproduced)", ""]
    if confirmed:
        for finding in confirmed:
            lines += [
                f"### {finding['id']} — {finding['title']}",
                "",
                f"- Severity: {finding['severity']}  |  Confidence: {finding['confidence']}  "
                f"|  Reproduced: {finding['reproducibility']}",
                f"- Feature: {finding['feature']}  |  Category: {finding['category']}",
                f"- Rule: {', '.join(finding.get('rule_ids') or []) or '-'}",
                f"- Expected: {finding.get('expected') or '-'}",
                f"- Actual: {finding.get('actual') or '-'}",
                f"- Steps: {'; '.join(finding.get('steps') or []) or '-'}",
                f"- Evidence: {json.dumps(finding.get('evidence') or {})[:600]}",
                f"- Remediation: {finding.get('remediation') or '-'}",
                "",
            ]
    else:
        lines.append("None.")
    unverified = [f for f in report["findings"] if f["status"] != "confirmed"]
    lines += ["", "## Unverified, not reproducible or needing human judgment", ""]
    if unverified:
        for finding in unverified:
            history = "; ".join(f"{e['actor']} → {e['action']}" for e in finding["events"][-4:])
            lines.append(f"- **{finding['id']}** [{finding['status']}] {finding['title']} — {history}")
    else:
        lines.append("None.")
    lines += ["", "## Invariants", ""]
    for invariant in report["invariants"]:
        lines.append(
            f"- **{invariant['id']}** ({invariant.get('kind') or 'unclassified'}) "
            f"[{invariant.get('origin') or 'explicit'}, confidence {invariant.get('confidence')}]: "
            f"{invariant['description']} — source {invariant['source']}")
    if not report["invariants"]:
        lines.append("No documented invariants were found.")
    lines += ["", "## Mission log", ""]
    for mission in report["missions"]:
        lines.append(f"- {mission['id']} [{mission['status']}] {mission['title']} ({mission['kind']})")
    if not report["missions"]:
        lines.append("No missions were recorded.")
    lines.append("")
    return "\n".join(lines)


def write_report(project_id: str, *, name: str | None = None,
                 directory: Path | None = None) -> dict[str, str]:
    report = build_report(project_id)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = directory or paths.reports_dir("phase2", project_id, "reports")
    folder.mkdir(parents=True, exist_ok=True)
    base = name or f"{stamp}-qa-report"
    json_path = folder / f"{base}.json"
    md_path = folder / f"{base}.md"
    json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    json_path.chmod(0o600)
    md_path.chmod(0o600)
    return {"json": ev.relative(json_path), "markdown": ev.relative(md_path)}


def _inline(values: dict[str, int]) -> str:
    return ", ".join(f"{key}: {value}" for key, value in sorted(values.items())) or "none"
