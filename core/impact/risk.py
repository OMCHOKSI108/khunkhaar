"""An additive, inspectable risk score for a change.

Six factors, each normalised to 0..1, each with a fixed weight, each reported with the raw signals
that produced it. The score is the sum of the weighted contributions on a 0..100 scale, and the
contributions are rounded before summing so the printed explanation always adds up to the printed
score. Nothing here is a statistical estimate: it is a prioritisation signal that orders changes,
and the wording that says so travels with every score, terminal or JSON.
"""

from __future__ import annotations

import re
from typing import Any

from core.impact import model, traverse

WORDING = ("This score is an approximate prioritisation signal, not a proven probability. "
           "It orders changes by attention, it does not predict failure.")

WEIGHTS: dict[str, float] = {
    "change complexity": 0.20,
    "dependency impact": 0.25,
    "business criticality": 0.20,
    "security sensitivity": 0.15,
    "historical bug density": 0.10,
    "test coverage gap": 0.10,
}

_SECURITY_TOKENS = frozenset({
    "auth", "authorize", "authorization", "authenticate", "authentication", "login", "logout",
    "password", "passwd", "token", "session", "permission", "permissions", "admin", "payment",
    "billing", "invoice", "crypto", "secret", "secrets", "oauth", "jwt", "acl", "role", "roles",
    "account", "checkout", "wallet", "transfer",
})
_STATUS_WEIGHT = {"confirmed": 1.0, "likely": 0.75, "unverified": 0.5, "needs_human": 0.5}
_SKIP_STATUS = {"false_positive", "not_reproducible"}
_TOKEN = re.compile(r"[A-Za-z0-9]+")


def score(*, graph: model.Graph, impact: traverse.ImpactResult, changeset: dict[str, Any],
          app_map: dict[str, Any] | None = None, findings: list[dict[str, Any]] | None = None,
          last_green: dict[str, Any] | None = None) -> dict[str, Any]:
    app_map = app_map or {}
    findings = findings or []
    summary = changeset.get("summary") or {}
    files = list(changeset.get("files") or [])
    changed_paths = {str(item.get("path")) for item in files if item.get("path")}
    changed_paths |= {str(item.get("old_path")) for item in files if item.get("old_path")}
    touched_paths = {item["path"] for item in impact.impacted if item.get("path")}

    factors = [
        _change_complexity(files, summary),
        _dependency_impact(impact),
        _business_criticality(impact),
        _security_sensitivity(changed_paths, touched_paths, app_map, findings),
        _historical_density(changed_paths, touched_paths, findings),
        _coverage_gap(impact, files, graph),
    ]
    score_value = round(sum(factor["contribution"] for factor in factors), 2)
    notes = [
        WORDING,
        "factor weights sum to 1.00 and each contribution is the factor's normalised value times "
        "its weight on a 0..100 scale, so the listed contributions add up to the score",
    ]
    if impact.truncated:
        notes.append("the impact walk was truncated; dependency impact is a lower bound")
    if not impact.seeds:
        notes.append("no changed file could be traced in the graph; reachability factors are "
                     "zero because nothing could be followed, which is not evidence of low risk")
    if last_green is None:
        notes.append("no passed regression run is recorded for this project; the coverage factor "
                     "uses graph links only")
    if graph.truncated:
        notes.append("the dependency graph itself was truncated at its file limit; the impact set "
                     "may be incomplete")

    return {
        "score": score_value,
        "scale": "0-100",
        "wording": WORDING,
        "attention_band": _band(score_value),
        "band_note": "a bucketed restatement of the same score, not a second estimate",
        "factors": factors,
        "inputs": {
            "changed_files": summary.get("files_changed", len(files)),
            "churn": int(summary.get("insertions", 0)) + int(summary.get("deletions", 0)),
            "seeds": len(impact.seeds),
            "impacted": len(impact.impacted),
            "tests_linked": len(impact.tests),
            "findings_considered": len([f for f in findings
                                        if f.get("status") not in _SKIP_STATUS]),
            "last_green_regression_run": (last_green or {}).get("run_id"),
            "graph_generated_at": graph.generated_at,
        },
        "notes": notes,
    }


def _factor(name: str, normalized: float, signals: dict[str, Any], direction: str) -> dict:
    normalized = max(0.0, min(1.0, float(normalized)))
    contribution = round(WEIGHTS[name] * normalized * 100.0, 2)
    return {"name": name, "weight": WEIGHTS[name], "normalized": round(normalized, 4),
            "contribution": contribution, "signals": signals, "direction": direction}


def _change_complexity(files: list[dict[str, Any]], summary: dict[str, Any]) -> dict:
    changed = int(summary.get("files_changed", len(files)))
    insertions = int(summary.get("insertions", 0))
    deletions = int(summary.get("deletions", 0))
    churn = insertions + deletions
    hunks = sum(int(item.get("hunks") or 0) for item in files)
    binary = sum(1 for item in files if item.get("binary"))
    renames = int(summary.get("renames", 0))
    normalized = (0.45 * min(1.0, churn / 400.0)
                  + 0.35 * min(1.0, changed / 25.0)
                  + 0.20 * min(1.0, hunks / 40.0))
    return _factor(
        "change complexity", normalized,
        {"files_changed": changed, "insertions": insertions, "deletions": deletions,
         "hunks": hunks, "renames": renames, "binary_files": binary,
         "saturates_at": {"churn": 400, "files": 25, "hunks": 40}},
        "rises with the number of files, lines and hunks the change touches; larger edits are "
        "harder to review and likelier to disturb unrelated behaviour")


def _dependency_impact(impact: traverse.ImpactResult) -> dict:
    summary = impact.summary
    impacted = int(summary.get("impacted", 0))
    surface = sum(int(value) for value in (summary.get("surface") or {}).values())
    normalized = 0.6 * min(1.0, impacted / 40.0) + 0.4 * min(1.0, surface / 20.0)
    if impact.truncated:
        normalized = max(normalized, 0.8)
    if not impact.seeds:
        normalized = 0.0
    return _factor(
        "dependency impact", normalized,
        {"impacted_nodes": impacted, "surface_nodes": surface,
         "direct_dependents": sum(1 for item in impact.impacted if item.get("depth") == 1),
         "truncated": impact.truncated,
         "saturates_at": {"impacted": 40, "surface": 20}},
        "rises with how much of the graph reaches the change and how many public interfaces "
        "(API, entity, workflow, journey) sit next to it; a change nothing depends on is cheap "
        "to make wrong")


def _business_criticality(impact: traverse.ImpactResult) -> dict:
    summary = impact.summary
    apis = int((summary.get("surface") or {}).get("api", 0))
    workflows = int(summary.get("workflows", 0))
    journeys = int(summary.get("journeys", 0))
    normalized = (0.35 * min(1.0, apis / 10.0)
                  + 0.40 * min(1.0, workflows / 4.0)
                  + 0.25 * min(1.0, journeys / 2.0))
    return _factor(
        "business criticality", normalized,
        {"endpoints_surfaced": apis, "workflows_reached": workflows,
         "journeys_reached": journeys, "saturates_at": {"apis": 10, "workflows": 4,
                                                        "journeys": 2}},
        "rises when the change touches endpoints, saved journeys or named features; these are "
        "the paths an operator's business actually runs on")


def _security_sensitivity(changed_paths: set[str], touched_paths: set[str],
                          app_map: dict[str, Any], findings: list[dict[str, Any]]) -> dict:
    auth_signals = [str(signal) for signal in (app_map.get("auth_signals") or [])]
    matched_paths = sorted(path for path in changed_paths | touched_paths
                           if security_tokens(path))
    security_findings = [finding for finding in findings
                         if finding.get("category") == "security"
                         and finding.get("status") in ("confirmed", "likely")]
    normalized = (0.4 * (1.0 if auth_signals else 0.0)
                  + 0.4 * min(1.0, len(matched_paths) / 3.0)
                  + 0.2 * min(1.0, len(security_findings) / 2.0))
    return _factor(
        "security sensitivity", normalized,
        {"auth_signals_in_app_map": len(auth_signals),
         "auth_signal_names": auth_signals[:5],
         "security_related_paths": matched_paths[:10],
         "open_security_findings": len(security_findings),
         "path_tokens": sorted(_SECURITY_TOKENS)},
        "rises when the application map saw authentication and when the touched files sit under "
        "paths whose names concern identity, money or administration; a name is a hint, not proof "
        "that the code handles a secret")


def _historical_density(changed_paths: set[str], touched_paths: set[str],
                        findings: list[dict[str, Any]]) -> dict:
    matched: list[dict[str, Any]] = []
    weighted = 0.0
    confirmed = 0
    for finding in findings:
        status = str(finding.get("status") or "")
        if status in _SKIP_STATUS:
            continue
        refs = {ref_path(ref) for ref in (finding.get("source_refs") or []) if ref}
        hit = sorted(refs & changed_paths)
        hit += sorted(refs & touched_paths - changed_paths)
        if not hit:
            continue
        weight = _STATUS_WEIGHT.get(status, 0.25)
        weighted += weight
        confirmed += 1 if status == "confirmed" else 0
        matched.append({"finding_id": finding.get("id") or finding.get("finding_id"),
                        "status": status, "weight": weight, "paths": sorted(set(hit))[:4]})
    normalized = min(1.0, weighted / 4.0)
    return _factor(
        "historical bug density", normalized,
        {"matched_findings": len(matched), "confirmed_matches": confirmed,
         "weighted_count": round(weighted, 2),
         "status_weights": _STATUS_WEIGHT, "saturates_at": 4,
         "examples": matched[:5]},
        "rises when earlier findings name files this change touches; a previous defect in the "
        "same file is relevant evidence, not proof that one returns")


def _coverage_gap(impact: traverse.ImpactResult, files: list[dict[str, Any]],
                  graph: model.Graph) -> dict[str, Any]:
    source_paths = sorted({str(item["path"]) for item in impact.seeds
                           if item.get("path") and _seed_is_source(graph, item)})
    covered_paths: set[str] = set()
    for test in impact.tests:
        for node_id in test.get("covers") or []:
            node = graph.node(node_id)
            if node is None:
                continue
            path = node.path or model.path_of(node_id)
            if path:
                covered_paths.add(path)
    uncovered = [path for path in source_paths if path not in covered_paths]
    source_changes = [item for item in files if item.get("path") in source_paths]
    normalized = (len(uncovered) / len(source_paths)) if source_paths else 0.0
    return _factor(
        "test coverage gap", normalized,
        {"source_files_changed": len(source_paths),
         "covered_by_linked_tests": len(source_paths) - len(uncovered),
         "without_linked_test": len(uncovered),
         "uncovered_examples": uncovered[:10],
         "source_files_with_hunks": sum(1 for item in source_changes
                                        if int(item.get("hunks") or 0))},
        "rises when changed source files have no linked test in the graph; a missing link is "
        "either a missing test or an unparsed test, and both reduce the confidence that a "
        "regression would be caught")


def _seed_is_source(graph: model.Graph, seed: dict[str, Any]) -> bool:
    """True when the seed is a file/test node that a parser could read.

    A path often has several seeds (file, class, symbol); testing only the last one would let a
    symbol seed hide the file seed and make a changed source file look untested and uncounted.
    """
    if seed.get("type") not in ("file", "test"):
        return False
    node = graph.node(str(seed.get("node_id") or ""))
    if node is None or node.type not in ("file", "test"):
        return False
    return node.meta.get("parsed") is not False


def security_tokens(path: str) -> list[str]:
    """Path tokens that name identity, money or administration, used as a hint only."""
    tokens = [token.lower() for token in _TOKEN.findall(path or "")]
    return [token for token in tokens if token in _SECURITY_TOKENS]


def ref_path(ref: Any) -> str:
    """`src/app.py:42` and `src/app.py` both name the same file for this purpose."""
    text = str(ref or "").strip()
    if text and text.rsplit(":", 1)[-1].isdigit():
        return text.rsplit(":", 1)[0]
    return text


def _band(value: float) -> str:
    if value >= 70:
        return "high attention"
    if value >= 45:
        return "elevated attention"
    if value >= 20:
        return "moderate attention"
    return "low attention"
