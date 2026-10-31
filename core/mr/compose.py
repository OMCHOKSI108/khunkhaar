"""Compose the Module B merge-request view from facts the other modules already produced.

Nothing here talks to a network, writes to a repository or executes a check. The input is a
read-only change set (Module A), the dependency graph and reverse walk (Module C), the risk
factors, the Phase 2 findings store and the optional GitLab metadata fetch. The output groups
those facts into what a reviewer asks for: modules, services, endpoints, GraphQL operations,
models, workflows, security implications, candidate regressions and a suggested QA plan.

Recommendations carry `status: recommended - not executed`. The plan is derived from the graph and
the risk factors, not from an LLM, and it is deliberately a list of checks a human may approve -
treating a recommendation as a test result is exactly the confusion this project forbids.
"""

from __future__ import annotations

from typing import Any

from core.impact import model as graph_model
from core.impact import risk as impact_risk

SUGGESTION_STATUS = "recommended - not executed"
_ARCHIVED = {"false_positive", "not_reproducible"}
_EVIDENCE = {"confirmed", "likely"}


def build(*, project_id: str, repository: str, changeset: dict[str, Any],
          graph: graph_model.Graph, impact: Any, risk: dict[str, Any],
          app_map: dict[str, Any], findings: list[dict[str, Any]],
          gitlab: dict[str, Any] | None = None) -> dict[str, Any]:
    changed, touched = _paths(changeset, impact)
    modules = _modules(changeset.get("files") or [])
    affected = _affected(graph, impact)
    security = _security(risk, changed, touched, affected, findings)
    regressions = _regressions(changed, touched, findings, impact, risk)
    plan = _plan(risk, impact, affected, security, regressions)
    notes = [
        "each affected item names the relation that reached it; an item whose origin is "
        "inferred is a parser or naming-convention guess, not a fact",
        "the dependency graph describes the current checkout, not necessarily the merge-request "
        "source revision; a branch that is not checked out can make these links stale",
        "the recommended QA plan was not executed; running any of it goes through the Phase 3 "
        "planner and its approval gate",
    ]
    if impact.missing:
        notes.append(f"{len(impact.missing)} changed path(s) are not represented in the graph; "
                     f"their impact could not be traced and is not zero")
    if changeset.get("conflicted"):
        notes.append("the repository is mid-merge; the change set is unreliable until resolved")
    if not changed:
        notes.append("the change set is empty, so there is nothing to review")
    return {
        "project_id": project_id,
        "repository": repository,
        "merge_request": {
            "kind": changeset.get("kind"),
            "target": changeset.get("base"),
            "source": changeset.get("head"),
            "base_source": changeset.get("base_source"),
            "summary": changeset.get("summary") or {},
            "conflicted": bool(changeset.get("conflicted")),
            "notes": list(changeset.get("notes") or []),
        },
        # Saved under this key so the artifact keeps the same change-set contract as
        # `git analyze` and a later `khunkhaar impact` can read it without re-analysing.
        "changeset": changeset,
        "modules": modules,
        "affected": affected,
        "security": security,
        "regressions": regressions,
        "risk": risk,
        "recommended_qa_plan": plan,
        "plan_execution": "none - the list is a recommendation, not a result",
        "graph": graph.stats(),
        "impact_summary": impact.summary,
        "gitlab": gitlab or {"status": "disabled",
                             "reason": "the GitLab adapter was not consulted",
                             "merge_request": None},
        "notes": notes,
        "saved": False,
        "artifact": None,
        "report_id": None,
        "analysis_id": None,
    }


def _paths(changeset: dict[str, Any], impact: Any) -> tuple[set[str], set[str]]:
    changed = {str(item.get("path")) for item in changeset.get("files") or []
               if item.get("path")}
    changed |= {str(item.get("old_path")) for item in changeset.get("files") or []
                if item.get("old_path")}
    touched = {item["path"] for item in impact.seeds if item.get("path")}
    touched |= {item["path"] for item in impact.impacted if item.get("path")}
    return changed, touched


def _modules(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for item in files:
        path = str(item.get("path") or "")
        module = path.split("/", 1)[0] if "/" in path else "(root)"
        entry = groups.setdefault(module, {
            "module": module, "files": 0, "insertions": 0, "deletions": 0,
            "symbols": 0, "languages": set(), "sample_paths": [], "symbol_names": [],
        })
        entry["files"] += 1
        entry["insertions"] += int(item.get("insertions") or 0)
        entry["deletions"] += int(item.get("deletions") or 0)
        entry["symbols"] += len(item.get("symbols") or [])
        if item.get("language"):
            entry["languages"].add(str(item["language"]))
        if len(entry["sample_paths"]) < 12:
            entry["sample_paths"].append(path)
        for symbol in item.get("symbols") or []:
            name = str(symbol.get("name") or "")
            if name and len(entry["symbol_names"]) < 12:
                entry["symbol_names"].append(name)
    out = []
    for entry in groups.values():
        entry["languages"] = sorted(entry["languages"])
        out.append(entry)
    return sorted(out, key=lambda item: (item["insertions"] + item["deletions"], item["files"],
                                         item["module"]), reverse=True)


def _entry(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "node_id": item.get("node_id"), "name": item.get("name"), "path": item.get("path"),
        "depth": item.get("depth"), "via": item.get("via"),
        "confidence": item.get("confidence"), "origin": item.get("origin"),
        "inferred": bool(item.get("inferred")),
    }


def _affected(graph: graph_model.Graph, impact: Any) -> dict[str, list[dict[str, Any]]]:
    services = [_entry(item) for item in impact.impacted if item.get("type") == "service"]
    services += [_entry(item) for item in impact.seeds if item.get("type") == "service"]
    endpoints: list[dict[str, Any]] = []
    graphql: list[dict[str, Any]] = []
    entities: list[dict[str, Any]] = []
    for item in impact.surface:
        if item.get("type") == "api":
            node = graph.node(str(item.get("node_id")))
            protocol = str((node.meta or {}).get("protocol") or "") if node else ""
            entry = _entry(item)
            entry["protocol"] = protocol or "http"
            (graphql if protocol == "graphql" else endpoints).append(entry)
        elif item.get("type") == "entity":
            entities.append(_entry(item))
    return {
        "services": sorted({item["node_id"]: item for item in services}.values(),
                           key=lambda item: item["node_id"] or ""),
        "api_endpoints": sorted({item["node_id"]: item for item in endpoints}.values(),
                                key=lambda item: item["node_id"] or ""),
        "graphql_operations": sorted({item["node_id"]: item for item in graphql}.values(),
                                     key=lambda item: item["node_id"] or ""),
        "database_models": sorted({item["node_id"]: item for item in entities}.values(),
                                  key=lambda item: item["node_id"] or ""),
        "workflows": sorted({item["node_id"]: _entry(item) for item in impact.workflows}.values(),
                            key=lambda item: item["node_id"] or ""),
        "journeys": sorted({item["node_id"]: _entry(item) for item in impact.journeys}.values(),
                           key=lambda item: item["node_id"] or ""),
        "tests": sorted({item["node_id"]: {"node_id": item.get("node_id"),
                                           "name": item.get("name"),
                                           "path": item.get("path"),
                                           "covers": list(item.get("covers") or [])}
                         for item in impact.tests}.values(),
                        key=lambda item: item["node_id"] or ""),
    }


def _factor(risk: dict[str, Any], name: str) -> dict[str, Any]:
    return next((item for item in risk.get("factors") or [] if item.get("name") == name), {})


def _finding_refs(finding: dict[str, Any]) -> set[str]:
    return {impact_risk.ref_path(ref) for ref in (finding.get("source_refs") or []) if ref}


def _security(risk: dict[str, Any], changed: set[str], touched: set[str],
              affected: dict[str, list[dict[str, Any]]],
              findings: list[dict[str, Any]]) -> dict[str, Any]:
    signals = _factor(risk, "security sensitivity").get("signals") or {}
    matched_paths = sorted(path for path in changed | touched
                           if impact_risk.security_tokens(path))
    path_findings: list[dict[str, Any]] = []
    for finding in findings:
        if finding.get("category") != "security" or finding.get("status") not in _EVIDENCE:
            continue
        hit = sorted(_finding_refs(finding) & (changed | touched))
        if not hit:
            continue
        path_findings.append({
            "id": finding.get("id") or finding.get("finding_id"),
            "status": finding.get("status"),
            "severity": finding.get("severity"),
            "title": finding.get("title"),
            "paths": hit[:6],
        })
    implications: list[str] = []
    if signals.get("auth_signal_names"):
        implications.append(
            "the application map records authentication signals ("
            + ", ".join(signals["auth_signal_names"]) + "); authorization on the affected "
            "endpoints should be re-checked, because a changed handler can widen or bypass a "
            "role check")
    if matched_paths:
        implications.append(
            "the change touches paths whose names concern identity, money or administration: "
            + ", ".join(matched_paths[:8])
            + "; a path name is a hint, not proof that the code handles a secret")
    if path_findings:
        implications.append(
            f"{len(path_findings)} confirmed/likely security finding(s) reference touched paths: "
            + ", ".join(str(item["id"]) for item in path_findings[:8]))
    if not implications:
        implications.append(
            "no authentication signal, security-named path or confirmed/likely security finding "
            "matched this change; that is absence of evidence, not evidence of safety")
    return {
        "implications": implications,
        "auth_signals": list(signals.get("auth_signal_names") or []),
        "security_related_paths": matched_paths,
        "findings": path_findings,
        "affected_endpoints": len(affected["api_endpoints"]) + len(affected["graphql_operations"]),
    }


def _regressions(changed: set[str], touched: set[str], findings: list[dict[str, Any]],
                 impact: Any, risk: dict[str, Any]) -> dict[str, Any]:
    historical: list[dict[str, Any]] = []
    for finding in findings:
        status = str(finding.get("status") or "")
        if status in _ARCHIVED:
            continue
        hit = sorted(_finding_refs(finding) & (changed | touched))
        if not hit:
            continue
        historical.append({
            "id": finding.get("id") or finding.get("finding_id"),
            "status": status,
            "severity": finding.get("severity"),
            "category": finding.get("category"),
            "title": finding.get("title"),
            "paths": hit[:6],
        })
    coverage = _factor(risk, "test coverage gap").get("signals") or {}
    linked_tests = [_entry(item) for item in impact.tests]
    notes: list[str] = []
    if not historical:
        notes.append("no earlier finding references a changed or reached path; absent history is "
                     "not evidence that the change is safe")
    if not linked_tests:
        notes.append("no test in the graph reaches the changed code; this may mean a missing "
                     "test or an unparsed test directory, and both lower confidence")
    if impact.missing:
        notes.append(f"{len(impact.missing)} changed path(s) could not be traced, so their "
                     f"regression surface is unknown")
    return {"historical_findings": historical, "linked_tests": linked_tests,
            "coverage": coverage, "notes": notes}


def _plan(risk: dict[str, Any], impact: Any, affected: dict[str, list[dict[str, Any]]],
          security: dict[str, Any], regressions: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    risk_score = float(risk.get("score") or 0)
    high_attention = risk_score >= 45

    def add(area: str, action: str, why: str, priority: str) -> None:
        key = (area, action)
        if key in seen or len(items) >= 12:
            return
        seen.add(key)
        items.append({"area": area, "action": action, "why": why, "priority": priority,
                      "status": SUGGESTION_STATUS})

    if "absence of evidence" not in " ".join(security["implications"]) and \
            security["affected_endpoints"]:
        add("security", "re-check authentication and authorization on the affected endpoints",
            "an authentication signal or security-relevant path sits on a changed endpoint",
            "high" if security["findings"] or security["auth_signals"] else "medium")
    if security["findings"]:
        add("security", "re-run the verified reproductions for the security findings that "
                        "reference touched paths",
            "findings "
            + ", ".join(str(item["id"]) for item in security["findings"][:4])
            + " are confirmed or likely and share files with this change", "high")
    for entry in affected["api_endpoints"][:5]:
        add("api", f"exercise {entry['name']} with valid, invalid and unauthenticated requests",
            f"reached by {entry['via']}", "high" if high_attention else "medium")
    for entry in affected["graphql_operations"][:5]:
        add("graphql", f"execute {entry['name']} with valid and invalid inputs",
            f"reached by {entry['via']}", "high" if high_attention else "medium")
    for entry in affected["database_models"][:5]:
        add("database", f"verify a read/write round-trip and schema compatibility for "
                        f"{entry['name']}",
            f"reached by {entry['via']}; no destructive statement is part of this check",
            "high" if high_attention else "medium")
    for entry in affected["workflows"][:3]:
        add("workflow", f"replay the {entry['name']} workflow end to end, including one "
                        f"controlled mutation",
            f"the workflow is triggered by an affected endpoint "
            f"(depth {entry.get('depth')})", "high" if high_attention else "medium")
    for entry in affected["journeys"][:2]:
        add("journey", f"replay the saved journey {entry['name']}",
            "the journey is triggered by an affected endpoint", "medium")
    for entry in affected["services"][:3]:
        add("service", f"review the change in {entry['name']} and its direct callers",
            "the service is in the reverse dependency walk", "low")
    if affected["tests"]:
        paths = ", ".join(str(item.get("path") or item.get("name"))
                          for item in affected["tests"][:6])
        add("regression", f"run the linked tests: {paths}",
            "these tests already reach the changed code in the graph", "medium")
    elif regressions["coverage"].get("without_linked_test"):
        examples = ", ".join(regressions["coverage"].get("uncovered_examples") or [])
        add("regression", "add or link a test for the changed source files"
            + (f" ({examples})" if examples else ""),
            "no test in the graph reaches the changed code", "high" if high_attention else "medium")
    for finding in regressions["historical_findings"]:
        if finding["status"] != "confirmed":
            continue
        add("history", f"re-run the verified reproduction for finding {finding['id']}",
            f"a confirmed finding references {', '.join(finding['paths'][:3])}", "high")
    if impact.missing:
        add("review", "manually review the changed paths that are not represented in the graph",
            f"{len(impact.missing)} path(s) could not be traced (see the impact report)", "medium")
    if risk_score >= 70:
        add("scope", "expand the run to the full regression suite before merge",
            f"the score is {risk_score:.2f}/100 ({risk.get('attention_band')}), which is an "
            f"attention signal, not a probability", "high")
    if not items:
        add("review", "review the change as an internal refactor",
            "the graph reaches no public surface for this change", "low")
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(items, key=lambda item: order.get(item["priority"], 3))
