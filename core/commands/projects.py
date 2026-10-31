"""`khunkhaar projects` — the registry view, with per-project QA totals."""

from __future__ import annotations

import argparse
import json

from core.findings import store as findings
from core.metadata import store
from core.shared import paths


def _rows() -> list[dict]:
    rows = []
    for project in store.list_projects():
        project_id = project["project_id"]
        stats = findings.findings_stats(project_id)
        with store.connect() as conn:
            analyses = conn.execute("SELECT count(*) AS n FROM git_analyses WHERE project_id=?",
                                    (project_id,)).fetchone()["n"]
            tests = conn.execute("SELECT count(*) AS n FROM regression_tests WHERE project_id=?",
                                 (project_id,)).fetchone()["n"]
            plans = conn.execute("SELECT count(*) AS n FROM qa_plans WHERE project_id=?",
                                 (project_id,)).fetchone()["n"]
            schedules = conn.execute(
                "SELECT count(*) AS n FROM monitoring_schedules WHERE project_id=? AND enabled=1",
                (project_id,)).fetchone()["n"]
        rows.append({
            "project_id": project_id,
            "name": project["name"],
            "root": project["root_path"],
            "type": project["project_type"],
            "last_seen": project["last_seen"],
            "profile_path": project["profile_path"],
            "findings": stats.get("total", 0),
            "confirmed": (stats.get("by_status") or {}).get("confirmed", 0),
            "git_analyses": analyses,
            "regression_tests": tests,
            "plans": plans,
            "schedules": schedules,
            "authorizations": len(store.active_authorizations(project_id)),
        })
    return rows


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="khunkhaar projects")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--paths", action="store_true", help="include the profile path column")
    args = parser.parse_args(argv)

    store.init()
    rows = _rows()
    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return 0
    if not rows:
        print("no projects registered. Run: khunkhaar project register --path /path/to/repo")
        return 0
    print(f"{'PROJECT':<34}{'TYPE':<16}{'FIND':<6}{'CONF':<6}{'GIT':<5}{'REG':<5}{'AUTH':<6}ROOT")
    for row in rows:
        print(f"{row['project_id']:<34}{str(row['type'] or '-'):<16}{row['findings']:<6}"
              f"{row['confirmed']:<6}{row['git_analyses']:<5}{row['regression_tests']:<5}"
              f"{row['authorizations']:<6}{row['root']}")
        if args.paths:
            print(f"{'':<34}profile: {row['profile_path']}")
    print(f"\n{len(rows)} project(s). Isolation: every row above is queried by its own "
          f"project_id; nothing is shared between projects except global knowledge.")
    print(f"evidence roots: {paths.evidence_dir()} | reports: {paths.reports_dir()}")
    return 0
