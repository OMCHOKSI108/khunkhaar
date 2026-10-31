"""`khunkhaar knowledge` - build, show and query this project's historical knowledge.

Building writes two 0600 artifacts under the KHUNKHAAR root and refreshes the index rows. Showing
and retrieving only read the stores; neither sends a request or writes to the repository.
"""

from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path

from core.knowledge import project as project_knowledge
from core.knowledge import retrieve as retrieve_module
from core.metadata import store
from core.project_discovery import detector

# `global` is a Python keyword, so the module is imported by name rather than with a dotted path.
global_knowledge = importlib.import_module("core.knowledge.global")


def _resolve_path(raw: str) -> Path:
    return Path(raw).expanduser().resolve()


def _require_project(prefix: str, root: Path) -> tuple[str | None, str | None]:
    project_id = detector.project_id_for(root)
    if project_id and store.get_project(project_id):
        return project_id, None
    return None, (f"khunkhaar {prefix}: {root} is not a registered project. Run "
                  f"`khunkhaar project register --path {root}` first.")


def build(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="khunkhaar knowledge build")
    parser.add_argument("--path", default=".", help="registered repository")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    store.init()
    root = _resolve_path(args.path)
    project_id, refusal = _require_project("knowledge build", root)
    if refusal:
        print(refusal)
        return 2

    result = project_knowledge.save(project_id)
    techniques = global_knowledge.save()
    document = result["document"]
    payload = {
        "project_id": project_id,
        "json": result["json"],
        "markdown": result["markdown"],
        "knowledge_item": result["item"],
        "global": {"json": techniques["json"], "markdown": techniques["markdown"],
                   "techniques": techniques["techniques"]},
        "sections": {key: (len(value) if isinstance(value, list) else value)
                     for key, value in document.items()
                     if key in ("confirmed_bugs", "resolved_bugs", "recurring_patterns",
                                "invariants", "workflows", "api_behaviours",
                                "permission_boundaries", "risky_modules", "edge_cases",
                                "false_positive_patterns", "regression", "counts")},
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return 0
    print(f"project knowledge  {result['json']}")
    print(f"markdown digest    {result['markdown']}")
    print(f"global techniques  {techniques['json']} ({techniques['techniques']} techniques)")
    counts = document.get("counts") or {}
    findings = counts.get("findings") or {}
    print(f"findings           {findings.get('total', 0)} "
          f"{findings.get('by_status') or {}}")
    print(f"invariants         {counts.get('invariants', 0)}")
    print(f"regression tests   {counts.get('regression_tests', 0)}")
    print("knowledge is derived from this project's own rows; nothing was executed")
    return 0


def show(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="khunkhaar knowledge show")
    parser.add_argument("--path", default=".", help="registered repository")
    parser.add_argument("--section", help="print one section (for example confirmed_bugs)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    store.init()
    root = _resolve_path(args.path)
    project_id, refusal = _require_project("knowledge show", root)
    if refusal:
        print(refusal)
        return 2

    document = project_knowledge.build(project_id)
    if args.section:
        if args.section not in document:
            print(f"khunkhaar knowledge show: unknown section '{args.section}'. Available: "
                  + ", ".join(sorted(document)))
            return 2
        print(json.dumps(document[args.section], indent=2, sort_keys=True, default=str))
        return 0
    if args.json:
        print(json.dumps(document, indent=2, sort_keys=True, default=str))
        return 0
    _print_document(document)
    return 0


def retrieve(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="khunkhaar knowledge retrieve")
    parser.add_argument("--path", default=".", help="registered repository")
    parser.add_argument("--changed", action="append", default=[],
                        help="changed path to rank history against (repeatable)")
    parser.add_argument("--limit", type=int, default=25, help="maximum history items")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    store.init()
    root = _resolve_path(args.path)
    project_id, refusal = _require_project("knowledge retrieve", root)
    if refusal:
        print(refusal)
        return 2

    changeset = {"changed_paths": args.changed} if args.changed else None
    payload = retrieve_module.retrieve(project_id, changeset, limit=max(1, args.limit))
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return 0
    paths = payload["changeset"].get("changed_paths") or []
    print(f"changed paths   {len(paths)}")
    for path in paths[:20]:
        print(f"  {path}")
    if payload.get("note"):
        print(f"note            {payload['note']}")
    for item in payload["items"]:
        print(f"{item['weight']:<6.2f}{item['kind']:<15}{item['id']:<16}{item.get('title')}")
        for contribution in item.get("contributions") or []:
            print(f"      +{contribution['weight']:<5.2f}{contribution['reason']}")
    for test in payload["tests"]:
        print(f"{test['weight']:<6.2f}{'test':<15}{test['test_id']:<16}"
              f"{test.get('reliability')} (finding {test.get('finding_id')})")
    print("weights are additive contributions, not probabilities")
    return 0


def _print_document(document: dict) -> None:
    project = document["project"]
    counts = document["counts"]
    findings = counts["findings"]
    print(f"project        {project.get('name') or project.get('project_id')} "
          f"({project.get('type') or 'unknown type'})")
    print(f"generated      {document['generated_at']}")
    print(f"findings       {findings.get('total', 0)} {findings.get('by_status') or {}}")
    print(f"invariants     {counts['invariants']}   workflows {counts['journeys']}   "
          f"regression tests {counts['regression_tests']}")
    for section, title in (("confirmed_bugs", "confirmed bugs"),
                           ("recurring_patterns", "recurring patterns"),
                           ("risky_modules", "risky modules")):
        value = document.get(section)
        print(f"\n{title}:")
        if isinstance(value, list):
            for row in value[:10]:
                print(f"  {row.get('finding_id') or row.get('pattern') or row.get('feature')}: "
                      f"{row.get('title') or row.get('pattern') or ''}")
        elif isinstance(value, dict):
            features = value.get("features") or []
            for row in features[:10]:
                print(f"  {row.get('feature')}: {row.get('findings')} "
                      f"{row.get('by_severity')}")
    regression = document.get("regression") or {}
    print(f"\nregression: {regression.get('tests', 0)} test(s), "
          f"{regression.get('runs', 0)} run(s), by result {regression.get('by_result') or {}}")
    print("\n" + "\n".join(f"- {note}" for note in document.get("notes") or []))
