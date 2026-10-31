"""Rank the project's own history against a change set.

The result is an auditable list, not a score: every item carries additive contributions and the
reason for each one, and the constants they come from travel with the result. A changed path that
a finding already points at outranks a finding that merely shares a feature; a failed-operation
integrity finding gets one extra contribution because a write-path change is exactly where that
class of defect recurs. The planner records these weighted reasons with its plan.
"""

from __future__ import annotations

import re
from typing import Any

from core.findings import store as findings_store
from core.impact import risk as impact_risk
from core.metadata import store
from core.shared.redaction import redact_obj

WEIGHTS: dict[str, float] = {
    "changed_path": 0.40,
    "same_feature": 0.10,
    "confirmed_status": 0.20,
    "likely_status": 0.10,
    "needs_human_status": 0.05,
    "high_severity": 0.10,
    "transaction_integrity": 0.15,
    "trusted_test": 0.10,
    "rejected_claim_context": 0.05,
}
PATH_HITS_CAPPED_AT = 2
_STATUS_WEIGHT = {"confirmed": "confirmed_status", "likely": "likely_status",
                  "needs_human": "needs_human_status"}
_HIGH_SEVERITY = {"critical", "high"}
_INTEGRITY = re.compile(
    r"\b(atomic|atomicity|transaction|rollback|partial(?:ly)?|audit record|orphan|"
    r"left behind|consisten(?:t|cy))\b", re.I)


def changed_paths(changeset: Any) -> list[str]:
    """Accept a full change set, a {"changed_paths": [...]} object, or a bare path list."""
    raw: list[Any] = []
    if isinstance(changeset, (list, tuple, set)):
        raw = list(changeset)
    elif isinstance(changeset, dict):
        raw = list(changeset.get("changed_paths") or [])
        for item in changeset.get("files") or []:
            if isinstance(item, dict):
                raw.extend([item.get("path"), item.get("old_path")])
            else:
                raw.append(item)
    normalized = {_normalize_path(impact_risk.ref_path(value)) for value in raw if value}
    return sorted(path for path in normalized if path)


def retrieve(project_id: str, changeset: Any = None, *, limit: int = 25) -> dict[str, Any]:
    paths = changed_paths(changeset)
    findings = findings_store.list_findings(project_id)
    invariants = findings_store.list_invariants(project_id)
    journeys = findings_store.list_journeys(project_id)
    tests = store.list_regression_tests(project_id)

    items: list[dict[str, Any]] = []
    finding_weights: dict[str, float] = {}
    for finding in findings:
        item = _finding_item(finding, paths)
        if item is None:
            continue
        finding_weights[str(finding["id"])] = item["weight"]
        items.append(item)

    for invariant in invariants:
        item = _invariant_item(invariant, paths)
        if item is not None:
            items.append(item)

    for journey in journeys:
        item = _journey_item(journey, paths)
        if item is not None:
            items.append(item)

    items.sort(key=lambda item: (-item["weight"], item["kind"], item["id"]))
    items = items[:max(1, limit)]

    test_items = [_test_item(test, finding_weights.get(str(test.get("finding_id")), 0.0))
                  for test in tests]
    test_items = [item for item in test_items if item["weight"] > 0]
    test_items.sort(key=lambda item: (-item["weight"], item["test_id"]))
    category_weights: dict[str, float] = {}
    for finding in findings:
        weight = finding_weights.get(str(finding["id"]), 0.0)
        if weight <= 0:
            continue
        category = str(finding.get("category") or "other")
        category_weights[category] = round(category_weights.get(category, 0.0) + weight, 2)

    payload = {
        "project_id": project_id,
        "changeset": {"changed_paths": paths, "supplied": changeset is not None,
                      "note": ("no changed paths were supplied; the result is the project's "
                               "standalone history, not an impact judgement") if not paths else
                              "weights are additive contributions from the constants below"},
        "weights": dict(WEIGHTS),
        "item_count": len(items),
        "tests": test_items,
        "bias": {
            "category_weights": dict(sorted(category_weights.items(),
                                            key=lambda pair: (-pair[1], pair[0]))),
            "test_order": [item["test_id"] for item in test_items],
            "note": ("category weights sum the item weights of confirmed, likely and needs_human "
                     "findings; they order attention, they are not probabilities"),
        },
        "items": items,
    }
    if not items:
        payload["note"] = ("no finding, invariant, journey or rejected claim in this project is "
                           "linked to the supplied paths or features")
    return redact_obj(payload)


def _finding_item(finding: dict[str, Any], paths: list[str]) -> dict[str, Any] | None:
    status = str(finding.get("status") or "")
    if status not in _STATUS_WEIGHT:
        if status not in ("false_positive", "not_reproducible"):
            return None
        return _rejected_item(finding, paths)
    contributions = []
    hits = _path_hits(finding.get("source_refs") or [], paths)
    feature = _feature_hit(finding.get("feature"), paths)
    if paths and not hits and not feature:
        return None
    if not paths and status != "confirmed":
        return None
    if hits:
        contributions.append(_path_contribution(hits))
    if feature:
        contributions.append({"factor": "same_feature", "weight": WEIGHTS["same_feature"],
                              "reason": f"the feature '{finding.get('feature')}' appears in a "
                                        f"changed path"})
    contributions.append({"factor": _STATUS_WEIGHT[status],
                          "weight": WEIGHTS[_STATUS_WEIGHT[status]],
                          "reason": f"the finding is {status}"})
    if str(finding.get("severity")) in _HIGH_SEVERITY:
        contributions.append({"factor": "high_severity", "weight": WEIGHTS["high_severity"],
                              "reason": f"severity is {finding.get('severity')}"})
    if (hits or feature) and _integrity_text(finding):
        contributions.append({
            "factor": "transaction_integrity", "weight": WEIGHTS["transaction_integrity"],
            "reason": "the finding is about failed-operation integrity (atomicity, rollback or "
                      "left-behind data) on a path the change touches",
        })
    weight = round(sum(item["weight"] for item in contributions), 2)
    return {
        "kind": "finding",
        "id": finding.get("id"),
        "title": finding.get("title"),
        "feature": finding.get("feature"),
        "category": finding.get("category"),
        "severity": finding.get("severity"),
        "status": status,
        "weight": weight,
        "contributions": contributions,
        "tests": [],
    }


def _rejected_item(finding: dict[str, Any], paths: list[str]) -> dict[str, Any] | None:
    hits = _path_hits(finding.get("source_refs") or [], paths)
    feature = _feature_hit(finding.get("feature"), paths)
    if not hits and not feature:
        return None
    contributions = []
    if hits:
        contributions.append(_path_contribution(hits))
    contributions.append({
        "factor": "rejected_claim_context", "weight": WEIGHTS["rejected_claim_context"],
        "reason": f"the claim was rejected as {finding.get('status')}; the rejection is kept as "
                  f"context so it is not reported again without new evidence",
    })
    events = findings_store.list_events(str(finding["id"]))
    event = next((item for item in reversed(events)
                  if item.get("action") == f"status:{finding.get('status')}"), {})
    return {
        "kind": "rejected-claim",
        "id": finding.get("id"),
        "title": finding.get("title"),
        "feature": finding.get("feature"),
        "category": finding.get("category"),
        "status": finding.get("status"),
        "reason": event.get("note") or "no reason was recorded with the status change",
        "weight": round(sum(item["weight"] for item in contributions), 2),
        "contributions": contributions,
    }


def _invariant_item(invariant: dict[str, Any], paths: list[str]) -> dict[str, Any] | None:
    source = invariant.get("source") or ""
    source_path = _normalize_path(impact_risk.ref_path(str(source).split(":", 1)[0]))
    hits = [source_path] if source_path and source_path in paths else []
    feature = _feature_hit(invariant.get("feature"), paths)
    if not hits and not feature and paths:
        return None
    contributions = []
    if hits:
        contributions.append(_path_contribution(hits))
    if feature:
        contributions.append({"factor": "same_feature", "weight": WEIGHTS["same_feature"],
                              "reason": f"the feature '{invariant.get('feature')}' appears in a "
                                        f"changed path"})
    if not contributions:
        contributions.append({"factor": "same_feature", "weight": 0.0,
                              "reason": "no change set was supplied; the invariant is listed as "
                                        "part of the project's baseline"})
    kind = _kind(invariant)
    if hits and kind in ("atomicity", "idempotency", "concurrency"):
        contributions.append({
            "factor": "transaction_integrity", "weight": WEIGHTS["transaction_integrity"],
            "reason": f"a {kind} invariant is attached to a changed path",
        })
    return {
        "kind": "invariant",
        "id": invariant.get("id"),
        "title": invariant.get("description"),
        "feature": invariant.get("feature"),
        "category": kind,
        "weight": round(sum(item["weight"] for item in contributions), 2),
        "contributions": contributions,
        "source": source,
    }


def _journey_item(journey: dict[str, Any], paths: list[str]) -> dict[str, Any] | None:
    step_paths = {_normalize_path(str(step.get("path") or ""))
                  for step in journey.get("steps") or [] if isinstance(step, dict)}
    hits = sorted(step_paths & set(paths))
    feature = _feature_hit(journey.get("feature"), paths)
    if not hits and not feature and paths:
        return None
    contributions = []
    if hits:
        contributions.append(_path_contribution(hits))
    if feature:
        contributions.append({"factor": "same_feature", "weight": WEIGHTS["same_feature"],
                              "reason": f"the workflow feature '{journey.get('feature')}' appears "
                                        f"in a changed path"})
    if not contributions:
        contributions.append({"factor": "same_feature", "weight": 0.0,
                              "reason": "no change set was supplied; the workflow is listed as "
                                        "part of the project's baseline"})
    return {
        "kind": "workflow",
        "id": journey.get("id"),
        "title": journey.get("name"),
        "feature": journey.get("feature"),
        "weight": round(sum(item["weight"] for item in contributions), 2),
        "contributions": contributions,
        "steps": [{key: step.get(key) for key in ("name", "method", "path", "actor", "expect")}
                  for step in journey.get("steps") or [] if isinstance(step, dict)],
    }


def _test_item(test: dict[str, Any], finding_weight: float) -> dict[str, Any]:
    reliability = str(test.get("reliability") or "generated")
    trusted = reliability == "trusted"
    weight = round(finding_weight + (WEIGHTS["trusted_test"] if trusted else 0.0), 2)
    contributions = []
    if finding_weight:
        contributions.append({
            "factor": "linked finding", "weight": finding_weight,
            "reason": f"the test guards finding {test.get('finding_id')}, whose history was "
                      f"selected by this change",
        })
    if trusted:
        contributions.append({
            "factor": "trusted_test", "weight": WEIGHTS["trusted_test"],
            "reason": "the test was promoted only after reproducing the defect and passing with "
                      "the expectation inverted",
        })
    return {
        "test_id": test.get("test_id"),
        "finding_id": test.get("finding_id"),
        "reliability": reliability,
        "method": test.get("method"),
        "objective": test.get("objective"),
        "weight": weight,
        "contributions": contributions,
    }


def _path_hits(refs: list[Any], paths: list[str]) -> list[str]:
    changed = set(paths)
    return sorted({_normalize_path(impact_risk.ref_path(ref)) for ref in refs
                   if ref and _normalize_path(impact_risk.ref_path(ref)) in changed})


def _path_contribution(hits: list[str]) -> dict[str, Any]:
    counted = min(len(hits), PATH_HITS_CAPPED_AT)
    return {
        "factor": "changed_path",
        "weight": round(WEIGHTS["changed_path"] * counted, 2),
        "reason": "the record references changed path(s): " + ", ".join(hits[:6]),
    }


def _feature_hit(feature: Any, paths: list[str]) -> bool:
    name = str(feature or "").strip().lower().replace("_", "-")
    if len(name) < 3 or name == "application":
        return False
    return any(name in _path_segments(path) for path in paths)


def _path_segments(path: str) -> str:
    return " / ".join(part for part in path.lower().replace("_", "-").split("/") if part)


def _integrity_text(finding: dict[str, Any]) -> bool:
    text = " ".join(str(finding.get(key) or "") for key in
                    ("title", "description", "expected", "actual"))
    return bool(_INTEGRITY.search(text))


def _kind(invariant: dict[str, Any]) -> str:
    from core.intelligence import rules as rule_classifier

    return rule_classifier.classify(str(invariant.get("description") or ""))


def _normalize_path(path: str) -> str:
    text = str(path or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text
