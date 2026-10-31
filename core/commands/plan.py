"""`khunkhaar plan` - build, review and approve a QA execution plan.

Building reads the checkout, the findings store, the latest saved change set, the dependency
graph, historical knowledge and eligible regression tests. It sends no request and executes no
test. The resulting artifact is the reviewed document; `khunkhaar qa --plan <id>` executes
exactly that document and refuses one whose digest changed.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from core.shared.target_policy import preflight_target
from core.metadata import store
from core.planning import planner
from core.project_discovery import detector
from core.shared.config import current


def _registered_project(root: Path, prefix: str) -> tuple[str | None, str | None]:
    project_id = detector.project_id_for(root)
    if project_id and store.get_project(project_id):
        return project_id, None
    return None, (f"khunkhaar {prefix}: {root} is not a registered project. Run "
                  f"`khunkhaar project register --path {root}` first.")


def _mode(requested: str | None) -> str:
    return requested or str(current().get("planner", "default_mode", "controlled"))


def build(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="khunkhaar plan")
    parser.add_argument("--path", default=".", help="registered repository")
    parser.add_argument("--target", help="base URL the plan's actions will use")
    parser.add_argument("--mode", choices=("observe", "controlled", "adversarial"))
    parser.add_argument("--impact-only", action="store_true",
                        help="keep only missions a changed path or impacted surface reaches")
    parser.add_argument("--max-actions", type=int, help="cap on proposed actions")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    store.init()

    root = Path(args.path).expanduser().resolve()
    project_id, refusal = _registered_project(root, "plan")
    if refusal:
        print(refusal)
        return 2
    if args.max_actions is not None and args.max_actions < 1:
        print("khunkhaar plan: --max-actions must be at least 1")
        return 2
    if args.target:
        refusal = preflight_target(project_id, args.target)
        if refusal:
            print(f"khunkhaar plan: {refusal}")
            return 2

    try:
        document = planner.build(project_id, root, target=args.target,
                                 mode=_mode(args.mode), impact_only=args.impact_only,
                                 max_actions=args.max_actions)
        planner.save(document)
    except planner.PlannerError as exc:
        print(f"khunkhaar plan: {exc}")
        return 2

    if args.json:
        print(json.dumps(document, indent=2, sort_keys=True, default=str))
        return 0
    _print_document(document)
    print(f"next            review with `khunkhaar plan show --plan {document['plan_id']}`, "
          f"approve with `khunkhaar plan approve --plan {document['plan_id']}`, then run "
          f"`khunkhaar qa --plan {document['plan_id']}`")
    return 0


def show(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="khunkhaar plan show")
    parser.add_argument("--plan", required=True, help="plan id")
    parser.add_argument("--limit", type=int, default=40, help="actions printed")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    store.init()
    try:
        document, _row = planner.load(args.plan)
    except planner.PlannerError as exc:
        print(f"khunkhaar plan show: {exc}")
        return 1
    if args.json:
        print(json.dumps(document, indent=2, sort_keys=True, default=str))
        return 0
    _print_document(document, limit=max(1, args.limit), rationale=True)
    return 0


def approve(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="khunkhaar plan approve")
    parser.add_argument("--plan", required=True, help="plan id")
    parser.add_argument("--by", default=os.environ.get("USER", "operator"),
                        help="name recorded with the approval")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    store.init()
    try:
        document = planner.approve(args.plan, approved_by=args.by)
    except planner.PlannerError as exc:
        print(f"khunkhaar plan approve: {exc}")
        return 1
    approval = document.get("approval") or {}
    payload = {"plan_id": document["plan_id"], "status": document["status"],
               "required": approval.get("required"),
               "approved_at": approval.get("approved_at"),
               "approved_by": approval.get("approved_by"),
               "artifact": document.get("artifact")}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return 0
    print(f"approved        {document['plan_id']} at {approval.get('approved_at')} "
          f"by {approval.get('approved_by')}")
    print(f"status          {document['status']}")
    print(f"artifact        {document.get('artifact')}")
    print("approval records a review of this exact digest; an edited plan is refused instead of "
          "executed")
    return 0


def list_plans(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="khunkhaar plan list")
    parser.add_argument("--path", default=".", help="registered repository")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    store.init()
    root = Path(args.path).expanduser().resolve()
    project_id, refusal = _registered_project(root, "plan list")
    if refusal:
        print(refusal)
        return 2
    rows = store.list_qa_plans(project_id, limit=max(1, args.limit))
    payload = {"project_id": project_id, "plans": rows, "count": len(rows)}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return 0
    if not rows:
        print("no plans are recorded for this project. Run "
              "`khunkhaar plan --path <repository> --target <url>` first.")
        return 0
    print(f"{'PLAN':<26}{'STATUS':<15}{'MODE':<13}{'ACTIONS':<9}{'APPROVED':<22}ARTIFACT")
    for row in rows:
        summary = row.get("summary") or {}
        approved = f"{row.get('approved_by') or '-'} {row.get('approved_at') or ''}".strip()
        print(f"{row['plan_id']:<26}{str(row.get('status')):<15}{str(row.get('mode')):<13}"
              f"{summary.get('actions', 0):<9}{approved[:21]:<22}{row.get('artifact')}")
    return 0


def _print_document(document: dict, *, limit: int = 40, rationale: bool = False) -> None:
    approval = document.get("approval") or {}
    inputs = document.get("inputs") or {}
    summary = document.get("summary") or {}
    estimated = summary.get("estimated") or {}
    changeset = inputs.get("changeset") or {}
    impact = (inputs.get("impact") or {}).get("summary") or {}
    knowledge = inputs.get("knowledge") or {}
    regression = inputs.get("regression") or {}

    print(f"plan            {document['plan_id']}  [{document.get('status')}]")
    print(f"artifact        {document.get('artifact')}")
    print(f"project         {document['project_id']}  ({document.get('repository')})")
    print(f"mode            {document.get('mode')}  target {document.get('target') or 'none'}")
    if changeset:
        changed = changeset.get("changed_paths") or []
        print(f"change set      {changeset.get('kind') or '-'} {changeset.get('base') or '-'} -> "
              f"{changeset.get('head') or '-'} ({len(changed)} path(s))")
    else:
        print("change set      none recorded")
    print(f"inputs          impact {impact.get('impacted', 'absent')} node(s); "
          f"knowledge {len(knowledge.get('items') or [])} item(s); "
          f"regression {len(regression.get('selected') or [])} test(s)")
    print(f"actions         {summary.get('actions', 0)} "
          f"(missions {summary.get('missions', 0)}, regression tests "
          f"{summary.get('regression_tests', 0)}, approval "
          f"{len(summary.get('approval_actions') or [])})")
    print(f"estimated       {estimated.get('steps', 0)} step(s), "
          f"{estimated.get('requests', 0)} request(s), "
          f"{estimated.get('sessions', 0)} browser session(s)")

    actions = document.get("actions") or []
    for action in actions[:limit]:
        cost = action.get("cost") or {}
        flag = "  APPROVAL" if action.get("requires_approval") else ""
        print(f"  {action['action_id']:<13}{action['kind']:<11}{action['agent']:<17}"
              f"{str(action.get('feature') or '-'):<16}"
              f"{cost.get('steps', 0)}s/{cost.get('requests', 0)}r/"
              f"{cost.get('sessions', 0)}b{flag}")
        if rationale:
            notes = action.get("rationale") or {}
            for reason in (notes.get("impact") or [])[:5]:
                print(f"    because       {reason}")
            if notes.get("knowledge_weight"):
                print(f"    knowledge     weight {notes['knowledge_weight']}")
            if action.get("approval_reason"):
                print(f"    approval      {action.get('approval_reason')}")
            tests = action.get("selected_tests") or []
            if tests:
                print(f"    tests         {', '.join(str(t.get('test_id')) for t in tests[:10])}")
    if len(actions) > limit:
        print(f"  ... {len(actions) - limit} more action(s); use --json for the full plan")
    if approval.get("required"):
        state = (f"granted at {approval.get('approved_at')} by {approval.get('approved_by')}"
                 if approval.get("approved_at") else "required, not granted")
        print(f"approval        {state}")
    else:
        print("approval        not required (no adversarial or external action)")
    for note in (inputs.get("notes") or [])[:10]:
        print(f"note            {note}")
    if not document.get("executable"):
        print(f"not executable  {document.get('executable_reason')}")
    for line in document.get("wording") or []:
        print(f"wording         {line}")
