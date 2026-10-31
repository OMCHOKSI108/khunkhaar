"""`khunkhaar qa` - execute a reviewed plan, or plan then execute an impact-only run.

Execution never regenerates missions: the stored plan's mission specifications and selected
regression test ids are reconstructed exactly. A plan whose digest changed is refused as
`plan drifted`, and a plan whose actions require approval runs only after an approval timestamp
is recorded on that digest.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from core.shared.target_policy import is_loopback, preflight_target
from core.metadata import store
from core.orchestrator.engine import Options, Orchestrator, OrchestratorError
from core.planning import planner
from core.project_discovery import detector
from core.runtime.budget import BudgetExceeded
from core.shared import evidence
from core.shared.config import current


def _operator() -> str:
    return os.environ.get("USER", "operator")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="khunkhaar qa")
    parser.add_argument("--plan", help="execute this stored plan exactly")
    parser.add_argument("--impact-only", action="store_true",
                        help="build and execute a plan restricted to impact-derived missions")
    parser.add_argument("--path", default=".", help="registered repository (--impact-only)")
    parser.add_argument("--target", help="base URL (--impact-only; a stored plan owns its target)")
    parser.add_argument("--mode", choices=("observe", "controlled", "adversarial"))
    parser.add_argument("--max-actions", type=int, help="cap on proposed actions (--impact-only)")
    parser.add_argument("--db", dest="db_path", help="SQLite database used for persistence checks")
    parser.add_argument("--users", dest="users_file", help="JSON role -> credentials file")
    parser.add_argument("--max-steps", type=int)
    parser.add_argument("--max-requests", type=int)
    parser.add_argument("--max-seconds", type=float)
    parser.add_argument("--max-browser-sessions", type=int)
    parser.add_argument("--no-verify", action="store_true")
    parser.add_argument("--approve", action="store_true",
                        help="record approval of this exact plan digest before executing it")
    parser.add_argument("--yes", action="store_true",
                        help="acknowledge adversarial testing against an authorized target")
    parser.add_argument("--json", action="store_true")
    return parser


def run(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    store.init()
    if bool(args.plan) == bool(args.impact_only):
        print("khunkhaar qa: pass exactly one of --plan <id> or --impact-only")
        return 2
    if args.plan and args.target:
        print("khunkhaar qa: a stored plan carries its own target; rebuild the plan to change it")
        return 2
    if args.impact_only and not args.target:
        print("khunkhaar qa: --impact-only requires --target because the resulting plan must be "
              "executable. Review it first with `khunkhaar plan --impact-only --target ...` if "
              "you only want the proposal.")
        return 2
    if args.plan:
        return _run_stored(args)
    return _run_impact_only(args)


def _run_stored(args: argparse.Namespace) -> int:
    try:
        document, _row = planner.load_for_execution(args.plan)
    except planner.PlannerError as exc:
        print(f"khunkhaar qa: {exc}")
        return 2
    document, refusal = _approve(args, document)
    if refusal:
        print(f"khunkhaar qa: {refusal}")
        return 2
    return _execute(args, document)


def _run_impact_only(args: argparse.Namespace) -> int:
    root = Path(args.path).expanduser().resolve()
    project_id = detector.project_id_for(root)
    if not project_id or not store.get_project(project_id):
        print(f"khunkhaar qa: {root} is not a registered project. Run "
              f"`khunkhaar project register --path {root}` first.")
        return 2
    refusal = preflight_target(project_id, args.target)
    if refusal:
        print(f"khunkhaar qa: {refusal}")
        return 2
    mode = args.mode or str(current().get("planner", "default_mode", "controlled"))
    try:
        document = planner.build(project_id, root, target=args.target, mode=mode,
                                 impact_only=True, max_actions=args.max_actions)
        planner.save(document)
    except planner.PlannerError as exc:
        print(f"khunkhaar qa: {exc}")
        return 2
    if not args.json:
        print(f"plan            {document['plan_id']}  [{document['status']}] "
              f"built from the change set")
        print(f"artifact        {document.get('artifact')}")
    if not document.get("actions"):
        note = ("the change set reached no mapped mission, regression test or impacted surface. "
                "The empty plan is saved for review; no run was recorded.")
        if args.json:
            print(json.dumps({"plan_id": document["plan_id"], "plan_status": document["status"],
                              "executed": False, "note": note,
                              "artifact": document.get("artifact")}, indent=2, sort_keys=True))
        else:
            print(f"nothing to execute: {note}")
        return 0
    document, refusal = _approve(args, document)
    if refusal:
        print(f"khunkhaar qa: {refusal}")
        return 2
    return _execute(args, document)


def _approve(args: argparse.Namespace, document: dict) -> tuple[dict | None, str | None]:
    approval = document.get("approval") or {}
    required = bool((document.get("summary") or {}).get("requires_approval"))
    if args.approve and not approval.get("approved_at"):
        return planner.approve(document["plan_id"], approved_by=_operator()), None
    if required and not approval.get("approved_at"):
        actions = (document.get("summary") or {}).get("approval_actions") or []
        return None, (f"plan '{document['plan_id']}' has {len(actions)} action(s) that require "
                      f"approval ({', '.join(str(a) for a in actions[:5])}). Review it with "
                      f"`khunkhaar plan show --plan {document['plan_id']}`, then approve with "
                      f"`khunkhaar plan approve --plan {document['plan_id']}` or pass --approve.")
    return document, None


def _execute(args: argparse.Namespace, document: dict) -> int:
    plan_id = document["plan_id"]
    project_id = document["project_id"]
    root = Path(document["repository"]).expanduser().resolve()
    if detector.project_id_for(root) != project_id or not store.get_project(project_id):
        print(f"khunkhaar qa: the plan's repository {root} no longer matches registered project "
              f"{project_id}; refusing to execute against a different checkout.")
        return 2
    target = document.get("target")
    mode = document.get("mode") or "controlled"
    if target:
        refusal = preflight_target(project_id, target)
        if refusal:
            print(f"khunkhaar qa: {refusal}")
            return 2
        if mode == "adversarial" and not is_loopback(target) and not args.yes:
            print("khunkhaar qa: adversarial mode against a shared host requires --yes")
            return 2

    run_id = evidence.new_run_id("qa")
    planner.mark(plan_id, "executing", run_id=run_id)
    options = Options(
        root=root, target=target, db_path=args.db_path, users_file=args.users_file,
        mode=mode, verify=not args.no_verify, max_steps=args.max_steps,
        max_requests=args.max_requests, max_seconds=args.max_seconds,
        max_browser_sessions=args.max_browser_sessions, command="qa", authorized=args.yes,
        plan_id=plan_id, planned_missions=planner.plan_missions(document),
        planned_regression=planner.plan_regression(document), run_id=run_id,
    )
    orchestrator = Orchestrator(options)
    try:
        summary = asyncio.run(orchestrator.run())
    except BudgetExceeded as exc:
        status = "cancelled" if getattr(exc, "kind", "") == "cancelled" else "budget_exceeded"
        planner.mark(plan_id, status, run_id=run_id)
        if args.json:
            print(json.dumps({"plan_id": plan_id, "plan_status": status, "run_id": run_id,
                              "summary": orchestrator.summary}, indent=2, sort_keys=True,
                             default=str))
        else:
            print(f"khunkhaar qa: stopped by budget - {exc}")
            _print_run(plan_id, status, orchestrator.summary)
        return 1
    except KeyboardInterrupt:
        row = store.get_run(run_id) or {}
        if row.get("status") == "running":
            store.finish_run(run_id, "interrupted", "interrupted by the operator")
        planner.mark(plan_id, "interrupted", run_id=run_id)
        print("khunkhaar qa: interrupted by the operator; the partial summary was kept.",
              file=sys.stderr)
        if args.json:
            print(json.dumps({"plan_id": plan_id, "plan_status": "interrupted",
                              "run_id": run_id, "summary": orchestrator.summary},
                             indent=2, sort_keys=True, default=str))
        return 130
    except OrchestratorError as exc:
        planner.mark(plan_id, "failed", run_id=run_id)
        print(f"khunkhaar qa: {exc}")
        return 2
    except Exception as exc:  # a failed run must be visible, never a silent success
        planner.mark(plan_id, "failed", run_id=run_id)
        print(f"khunkhaar qa: run failed - {type(exc).__name__}: {exc}")
        return 1

    run_status = (store.get_run(run_id) or {}).get("status") or "completed"
    planner.mark(plan_id, run_status, run_id=run_id)
    if args.json:
        print(json.dumps({"plan_id": plan_id, "plan_status": run_status, "run_id": run_id,
                          "summary": summary}, indent=2, sort_keys=True, default=str))
        return 0
    _print_run(plan_id, run_status, summary)
    return 0


def _print_run(plan_id: str, status: str, summary: dict) -> None:
    print(f"plan            {plan_id}  [{status}]")
    print(f"run             {summary.get('run_id')}")
    target = summary.get("target") or {}
    if target.get("connected"):
        print(f"target          {target.get('target')}  contract {target.get('contract')}  "
              f"operations {target.get('operations')}  database {target.get('database')}")
    regression = summary.get("regression") or {}
    if regression:
        execution = regression.get("execution") or {}
        counts = (execution.get("summary") or {}).get("by_result") or {}
        print(f"regression      executed {execution.get('run_id') or 'no'}  results {counts}  "
              f"missing {len(regression.get('missing') or [])}")
        if not regression.get("executed"):
            print(f"                {regression.get('note')}")
    missions = summary.get("missions") or {}
    if missions:
        print(f"missions        planned {missions.get('planned')}  executed "
              f"{missions.get('executed')}  skipped {missions.get('skipped')}  "
              f"failed {missions.get('failed')}")
        for note in (missions.get("skipped_detail") or [])[:5]:
            print(f"                skip: {note}")
    findings = summary.get("findings") or {}
    if findings:
        stats = summary.get("findings_stats") or {}
        print(f"findings        created {findings.get('created')}  "
              f"updated {findings.get('updated')}  by status {stats.get('by_status')}")
    resources = summary.get("resources") or {}
    if resources:
        print(f"resources       peak RSS {resources.get('peak_rss_kb')} KiB  "
              f"llm usage {resources.get('llm_usage')}")
    budget = summary.get("budget") or {}
    if budget:
        print(f"budget          steps {budget.get('steps')}/{budget.get('max_steps')}  "
              f"requests {budget.get('requests')}/{budget.get('max_requests')}  "
              f"sessions {budget.get('browser_sessions')}/{budget.get('max_browser_sessions')}")
    evidence_paths = summary.get("evidence") or {}
    if evidence_paths:
        print(f"evidence        {evidence_paths.get('run')}")
        print(f"log             {evidence_paths.get('log')}")
    print("a plan proposes; only this execution's recorded evidence supports a defect claim")
