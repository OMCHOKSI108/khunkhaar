"""`khunkhaar project register|show|authorize` — multi-project management entry points.

Registration makes a repository *analysable*. Authorisation makes a target *testable*. They are
separate commands on purpose: a company checkout can be indexed and diffed without KHUNKHAAR ever
being allowed to send a request to it.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.findings import store as findings
from core.metadata import store
from core.project_discovery import detector
from core.shared import paths
from core.shared.config import current


def _reject_production(target: str) -> str | None:
    security = current().section("security")
    if not security.get("forbid_production_targets", True):
        return None
    lowered = target.lower()
    markers = security.get("production_markers", []) or []
    hit = next((m for m in markers if str(m).lower() in lowered), None)
    return str(hit) if hit else None


def register(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="khunkhaar project register")
    parser.add_argument("--path", default=".", help="repository to register")
    parser.add_argument("--name", help="display name (default: directory name)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    store.init()

    root = Path(args.path).expanduser().resolve()
    if not root.is_dir():
        print(f"khunkhaar project register: {root} is not a directory")
        return 2
    if root == paths.kh_root():
        print("khunkhaar project register: refusing to register the KHUNKHAAR root itself")
        return 2

    profile = detector.detect(root)
    if not profile.get("valid_project"):
        print(f"khunkhaar project register: {profile.get('reason')}")
        return 2
    if args.name:
        profile["name"] = args.name
    # write_profile stores the registry row and the 0600 profile; nothing is written into `root`.
    profile_path = detector.write_profile(profile)
    result = {
        "project_id": profile["project_id"],
        "name": profile["name"],
        "root": profile["root"],
        "type": profile["project_type"],
        "profile_path": str(profile_path),
        "languages": [entry["name"] for entry in profile["languages"][:8]],
        "frameworks": profile["frameworks"][:8],
        "files_scanned": profile["scan"]["files_scanned"],
        "authorized_targets": [],
        "note": "registration enables read-only analysis only; testing requires "
                "'khunkhaar project authorize'",
    }
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    for key in ("project_id", "name", "root", "type", "profile_path", "files_scanned"):
        print(f"{key:<14}{result[key]}")
    print(f"{'languages':<14}{', '.join(result['languages']) or '-'}")
    print(f"{'frameworks':<14}{', '.join(result['frameworks']) or '-'}")
    print("authorized_targets none - analysis only. Add one with "
          "`khunkhaar project authorize --project-id ... --target ... --mode ...`")
    return 0


def show(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="khunkhaar project show")
    parser.add_argument("--project-id")
    parser.add_argument("--path")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    store.init()

    project_id = args.project_id
    if not project_id and args.path:
        project_id = detector.project_id_for(Path(args.path).expanduser().resolve())
    if not project_id:
        project_id = detector.detect(Path.cwd()).get("project_id")
    row = store.get_project(project_id or "")
    if not row:
        print(f"khunkhaar project show: {project_id!r} is not registered")
        return 1
    stats = findings.findings_stats(project_id)
    with store.connect() as conn:
        tables = {
            "git_analyses": "SELECT count(*) AS n FROM git_analyses WHERE project_id=?",
            "regression_tests": "SELECT count(*) AS n FROM regression_tests WHERE project_id=?",
            "qa_plans": "SELECT count(*) AS n FROM qa_plans WHERE project_id=?",
            "schedules": ("SELECT count(*) AS n FROM monitoring_schedules WHERE project_id=?"),
            "knowledge": ("SELECT count(*) AS n FROM knowledge_items WHERE scope='project' "
                          "AND project_id=?"),
            "evidence": "SELECT count(*) AS n FROM evidence_refs WHERE project_id=?",
        }
        counts = {name: conn.execute(sql, (project_id,)).fetchone()["n"]
                  for name, sql in tables.items()}
    payload = {
        "project": {k: row[k] for k in ("project_id", "name", "root_path", "profile_path",
                                        "project_type", "first_seen", "last_seen")},
        "findings": stats,
        "counts": counts,
        "authorizations": store.active_authorizations(project_id),
        "directories": {
            "registry": str(paths.project_dir(project_id)),
            "knowledge": str(paths.project_knowledge_dir(project_id)),
            "state": str(paths.project_state_dir(project_id)),
        },
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return 0
    for key, value in payload["project"].items():
        print(f"{key:<14}{value}")
    print(f"{'findings':<14}{stats.get('total', 0)} by status {stats.get('by_status')}")
    print("counts        " + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    authorizations = payload["authorizations"]
    print(f"authorizations {len(authorizations)} active")
    for entry in authorizations:
        print(f"  {entry['mode']:<12}{entry['target']}  granted {entry['granted_at']} "
              f"by {entry['granted_by']}  expires {entry['expires_at'] or 'never'}")
    for name, directory in payload["directories"].items():
        print(f"{name:<14}{directory}")
    return 0


def authorize(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="khunkhaar project authorize")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--target", required=True, help="base URL of an authorized test environment")
    parser.add_argument("--mode", required=True, choices=("observe", "controlled", "adversarial"))
    parser.add_argument("--days", type=int, help="expire the grant after N days")
    parser.add_argument("--note")
    parser.add_argument("--granted-by", default=os.environ.get("USER", "operator"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    store.init()

    if store.get_project(args.project_id) is None:
        print(f"khunkhaar project authorize: {args.project_id} is not registered")
        return 2
    marker = _reject_production(args.target)
    if marker:
        print(f"khunkhaar project authorize: refused - {args.target} looks like production "
              f"(matches {marker!r}). KHUNKHAAR does not authorise production targets.")
        return 2
    expires = None
    if args.days:
        expires = (datetime.now(timezone.utc) + timedelta(days=args.days)
                   ).isoformat(timespec="seconds")
    store.grant_authorization(args.project_id, args.target, args.mode,
                              granted_by=args.granted_by, expires_at=expires, note=args.note)
    result = {"project_id": args.project_id, "target": args.target, "mode": args.mode,
              "expires_at": expires, "granted_by": args.granted_by}
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    print(f"authorized {args.mode} testing of {args.target} for {args.project_id}")
    print(f"expires     {expires or 'never'}")
    print(f"granted by  {args.granted_by}")
    print("the grant is recorded in the metadata store; the planner still shows the plan before "
          "any test runs")
    return 0
