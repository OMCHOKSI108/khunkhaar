"""Build, store, approve and load QA execution plans.

Three inputs are merged and each contribution is written down:

* the impact result for the latest saved change set (dependency reachability and an approximate
  attention score that is explicitly not a probability),
* the project's historical knowledge, retrieved with the auditable weights of
  :mod:`core.knowledge.retrieve`,
* eligible regression tests and the Phase 2 mission generator's proposals.

Nothing here sends a request or executes a test. Planning reads the checkout, the store and the
saved analysis; execution is a separate, explicitly approved step.

The digest covers everything reviewed - inputs, actions, costs, rationale, approval requirement -
and excludes only bookkeeping fields (plan id, timestamps, status, approval record). Editing a
plan's meaning therefore changes its digest, and a changed digest is refused as `plan drifted`.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from core.findings import store as findings_store
from core.impact import graph as impact_graph
from core.impact import risk as impact_risk
from core.impact import traverse
from core.intelligence import mapper, rules
from core.knowledge import retrieve as knowledge_retrieve
from core.metadata import store
from core.missions import generator as mission_generator
from core.project_discovery import detector
from core.regression import runner as regression_runner
from core.shared import netguard, paths
from core.shared.config import current
from core.shared.redaction import redact_obj

SCHEMA = 1

# These mission kinds are GET-only probes in the shipped probe set, so a bounded worker pool may
# run them concurrently. Everything else - browser work, journeys, writes, database checks - stays
# serial. A mission is never marked parallel-safe by its agent name alone.
READ_ONLY_MISSION_KINDS = frozenset({"standalone:auth_boundary"})

AGENT_COST: dict[str, dict[str, int]] = {
    "api_hunter": {"steps": 6, "requests": 6, "sessions": 0},
    "edge_hunter": {"steps": 5, "requests": 5, "sessions": 0},
    "security_hunter": {"steps": 6, "requests": 8, "sessions": 0},
    "workflow_breaker": {"steps": 12, "requests": 10, "sessions": 0},
    "db_hunter": {"steps": 8, "requests": 6, "sessions": 0},
    "ux_inspector": {"steps": 5, "requests": 2, "sessions": 1},
    "user_simulator": {"steps": 24, "requests": 2, "sessions": 1},
    "regression_intel": {"steps": 3, "requests": 3, "sessions": 0},
}
DEFAULT_COST = {"steps": 6, "requests": 4, "sessions": 0}

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_VOLATILE = frozenset({"plan_id", "digest", "created_at", "status", "approval", "artifact",
                       "saved", "stored_rows"})
_WORDING = (
    "costs are planning estimates, not enforced budgets; execution is bounded by the mode budget",
    "a plan is a proposal: no request, test or browser session has been executed at plan time",
    "the impact score is an approximate prioritisation signal, not a probability",
    "a plan whose digest no longer matches is refused instead of executed",
)


class PlannerError(RuntimeError):
    """A plan cannot be built, loaded or approved. The message explains why."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def clean_target(target: str | None) -> str | None:
    """Record a target without credentials, query or fragment anywhere in the plan."""
    if not target:
        return None
    parts = urlsplit(str(target).strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise PlannerError("the target must be an http(s) URL with a host")
    netloc = netguard.normalize_host(parts.hostname)
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path or "", "", ""))


def is_loopback(target: str | None) -> bool:
    if not target:
        return True
    return netguard.normalize_host(urlsplit(target).hostname or "") in _LOOPBACK


def digest_of(document: dict[str, Any]) -> str:
    """Digest of everything reviewed; bookkeeping fields cannot change it."""
    reviewed = {key: value for key, value in document.items() if key not in _VOLATILE}
    canonical = json.dumps(redact_obj(reviewed), sort_keys=True, separators=(",", ":"),
                           default=str, ensure_ascii=False)
    return _sha(canonical)


def _planning_config() -> dict[str, Any]:
    section = current().section("planner") or {}
    return {
        "max_actions": max(1, int(section.get("max_actions", 80))),
        "api_workers": max(1, int(section.get("api_workers", 2))),
        "require_approval_for_adversarial": bool(
            section.get("require_approval_for_adversarial", True)),
        "require_approval_for_external_target": bool(
            section.get("require_approval_for_external_target", True)),
    }


def parallel_workers() -> int:
    """Bounded worker count for read-only API missions, capped by the run budget too."""
    return _planning_config()["api_workers"]


# ------------------------------------------------------------------ inputs


def _latest_changeset(project_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    row = store.latest_git_analysis(project_id)
    if not row or not row.get("artifact"):
        return row, None
    target = Path(str(row["artifact"]))
    if not target.is_absolute():
        target = paths.kh_root() / target
    try:
        resolved = target.resolve()
        resolved.relative_to(paths.kh_root().resolve())
        document = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return row, None
    changeset = document.get("changeset") if isinstance(document, dict) else None
    return row, changeset if isinstance(changeset, dict) else None


def _impact(project_id: str, root: Path, app_map: dict[str, Any], changeset: dict[str, Any] | None,
            analysis_row: dict[str, Any] | None,
            findings: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, set[str], list[str]]:
    """The impact result, the feature tokens it reached, and honest notes when it is absent."""
    if not changeset:
        return None, set(), ["no saved git analysis exists, so the plan has no impact input; run "
                             "`khunkhaar git analyze` to plan from a change set"]
    notes: list[str] = []
    graph = impact_graph.load(project_id)
    if graph is None:
        try:
            graph = impact_graph.build(project_id, root, app_map=app_map, persist=True)["graph"]
        except Exception as exc:  # planning must not pretend the graph was followed
            return None, set(), [f"the dependency graph could not be built "
                                 f"({type(exc).__name__}: {exc}); no impact input was used"]
    depth = int(current().get("impact", "max_depth", traverse.DEFAULT_DEPTH))
    max_nodes = int(current().get("impact", "max_impacted", traverse.DEFAULT_MAX_NODES))
    result = traverse.analyze(
        graph, project_id=project_id, changeset=changeset,
        analysis={"analysis_id": (analysis_row or {}).get("id"),
                  "kind": changeset.get("kind"), "base": changeset.get("base"),
                  "head": changeset.get("head")},
        depth=max(1, min(depth, 10)), max_nodes=max_nodes)
    risk = impact_risk.score(
        graph=graph, impact=result, changeset=changeset, app_map=app_map, findings=findings,
        last_green=store.last_green_regression_run(project_id))
    tokens: set[str] = set()
    for section in ("surface", "workflows", "journeys"):
        for item in getattr(result, section):
            for key in ("name", "path"):
                value = item.get(key)
                if not value:
                    continue
                text = str(value).lower()
                tokens.add(Path(text).stem if key == "path" else text)
    document = {
        "analysis": result.analysis,
        "summary": result.summary,
        "risk": {key: risk.get(key) for key in
                 ("score", "scale", "wording", "attention_band", "factors", "inputs", "notes")},
        "seeds": result.seeds[:20],
        "surface": result.surface[:40],
        "workflows": result.workflows[:20],
        "journeys": result.journeys[:20],
        "tests": result.tests[:20],
        "missing": result.missing[:20],
        "notes": result.notes + notes,
    }
    return document, tokens, notes


def _changed_paths(changeset: dict[str, Any] | None) -> list[str]:
    return knowledge_retrieve.changed_paths(changeset) if changeset else []


def _knowledge(project_id: str, changeset: dict[str, Any] | None) -> dict[str, Any]:
    result = knowledge_retrieve.retrieve(project_id, changeset, limit=25)
    return {
        "weights": result.get("weights"),
        "items": result.get("items") or [],
        "tests": result.get("tests") or [],
        "bias": result.get("bias"),
        "note": ("contributions are additive and auditable; the planner only uses them to order "
                 "proposals, it does not turn them into a probability"),
    }


def _knowledge_index(knowledge: dict[str, Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for item in knowledge.get("items") or []:
        if item.get("id"):
            index[str(item["id"])] = item
    return index


def _knowledge_for(mission, index: dict[str, dict[str, Any]]) -> tuple[list[dict], float]:
    ids = []
    invariant = mission.payload.get("invariant") or {}
    if invariant.get("id"):
        ids.append(str(invariant["id"]))
    if mission.payload.get("finding_id"):
        ids.append(str(mission.payload["finding_id"]))
    items = [index[item_id] for item_id in ids if item_id in index]
    weight = round(sum(float(item.get("weight") or 0.0) for item in items), 2)
    return items, weight


# ------------------------------------------------------------------ selection


def _invariant_path(mission) -> str:
    invariant = mission.payload.get("invariant") or {}
    source = str(invariant.get("source") or "")
    return impact_risk.ref_path(source.split(":", 1)[0])


def _finding_for(mission, findings: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return findings.get(str(mission.payload.get("finding_id") or "")) or {}


def _feature(mission, finding: dict[str, Any]) -> str:
    invariant = mission.payload.get("invariant") or {}
    return str(invariant.get("feature") or finding.get("feature")
               or mission.payload.get("feature") or "").strip()


def _feature_match(feature: str, tokens: set[str], changed: set[str]) -> str | None:
    if not feature or feature == "application":
        return None
    variants = {feature.lower(), feature.rstrip("s").lower()}
    for token in tokens:
        if any(variant and variant in token for variant in variants):
            return f"the impacted surface '{token}' matches feature '{feature}'"
    for path in changed:
        lowered = path.lower()
        if any(variant and variant in lowered for variant in variants):
            return f"a changed path '{path}' names feature '{feature}'"
    return None


def _impact_reasons(mission, *, findings: dict[str, dict[str, Any]], changed: set[str],
                    tokens: set[str]) -> list[str]:
    reasons: list[str] = []
    source_path = _invariant_path(mission)
    if source_path and source_path in changed:
        reasons.append(f"the invariant's source '{source_path}' is in the change set")
    finding = _finding_for(mission, findings)
    for ref in finding.get("source_refs") or []:
        ref_path = impact_risk.ref_path(ref)
        if ref_path in changed:
            reasons.append(f"the linked finding references changed path '{ref_path}'")
            break
    feature_reason = _feature_match(_feature(mission, finding), tokens, changed)
    if feature_reason:
        reasons.append(feature_reason)
    return reasons


def _approval(mode_required: str, target: str | None,
              config: dict[str, Any]) -> tuple[bool, str | None]:
    if config["require_approval_for_adversarial"] and mode_required == "adversarial":
        return True, "the action runs an adversarial, state-changing probe"
    if (config["require_approval_for_external_target"] and target
            and not is_loopback(target)):
        return True, "the action sends requests to an external (non-loopback) target"
    return False, None


def _cost(mission) -> dict[str, int]:
    cost = dict(AGENT_COST.get(mission.agent, DEFAULT_COST))
    if mission.kind == "exploration":
        cost["steps"] = int(mission.payload.get("max_steps") or cost["steps"])
    return cost


def _regression_selection(project_id: str, changeset: dict[str, Any] | None,
                          impact_only: bool) -> dict[str, Any]:
    if impact_only and not changeset:
        return {"selector": "impact", "selected": [], "excluded": [],
                "note": "an impact-only plan needs a saved change set; none exists"}
    if changeset:
        selection = regression_runner.select_by_impact(project_id, _changed_paths(changeset))
        selector = "impact"
    else:
        selection = regression_runner.select_all(project_id)
        selector = "all"
    return {
        "selector": selector,
        "selected": [{"test_id": item["test"]["test_id"], "reason": item.get("reason"),
                      "reliability": item["test"].get("reliability")}
                     for item in selection["selected"]],
        "excluded": selection["excluded"][:50],
        "note": ("selected tests are replayed by the plan; execution records a result per test "
                 "and an unavailable environment is never a defect"),
    }


def _action_id(kind: str, identity: str) -> str:
    return "A-" + _sha(f"{kind}|{identity}")[:10]


def _mission_action(mission, *, target: str | None, mode: str, reasons: list[str],
                    knowledge_items: list[dict], knowledge_weight: float,
                    findings: dict[str, dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    needs_approval, approval_reason = _approval(mission.mode_required, target, config)
    finding = _finding_for(mission, findings)
    mission.parallel_safe = mission.kind in READ_ONLY_MISSION_KINDS
    return {
        "action_id": _action_id("mission", mission.mission_id),
        "kind": "mission",
        "agent": mission.agent,
        "feature": _feature(mission, finding) or None,
        "target": target,
        "mode": mission.mode_required,
        "requires_approval": needs_approval,
        "approval_reason": approval_reason,
        "cost": _cost(mission),
        "rationale": {
            "selection": ("impact-derived" if reasons else "baseline mission generator"),
            "impact": reasons,
            "knowledge_weight": knowledge_weight,
            "knowledge": knowledge_items[:3],
            "task": mission.title,
            "kind": mission.kind,
            "parallel_safe": mission.parallel_safe,
            "estimate_note": "planning estimate; the mode budget is enforced at execution",
        },
        "mission": mission.to_dict(),
        "selected_tests": [],
    }


def _regression_action(project_id: str, selection: dict[str, Any], *, target: str | None,
                       mode: str, knowledge: dict[str, Any],
                       config: dict[str, Any]) -> dict[str, Any] | None:
    if not selection["selected"]:
        return None
    # A regression replay is bounded and controlled; only an external target needs an approval.
    needs_approval, approval_reason = _approval("controlled", target, config)
    selected_ids = [item["test_id"] for item in selection["selected"]]
    knowledge_tests = {item["test_id"]: item for item in knowledge.get("tests") or []}
    return {
        "action_id": _action_id("regression", ",".join(selected_ids)),
        "kind": "regression",
        "agent": "regression_intel",
        "feature": None,
        "target": target,
        "mode": mode,
        "requires_approval": needs_approval,
        "approval_reason": approval_reason,
        "cost": {"steps": len(selected_ids), "requests": 0, "sessions": 0},
        "rationale": {
            "selection": f"regression tests selected by '{selection['selector']}'",
            "impact": [item["reason"] for item in selection["selected"] if item.get("reason")],
            "knowledge": [knowledge_tests[test_id] for test_id in selected_ids
                          if test_id in knowledge_tests][:5],
            "estimate_note": "one bounded pytest process per selected test",
        },
        "mission": None,
        "selected_tests": [{"test_id": item["test_id"], "reason": item.get("reason")}
                           for item in selection["selected"]],
    }


# ------------------------------------------------------------------ build


def build(project_id: str, root: Path, *, target: str | None = None, mode: str = "controlled",
          impact_only: bool = False, max_actions: int | None = None) -> dict[str, Any]:
    """Assemble a plan document. Nothing here executes or sends anything."""
    config = _planning_config()
    limit = max(1, min(int(max_actions or config["max_actions"]), config["max_actions"]))
    clean = clean_target(target)
    root = Path(root).resolve()

    app_map_obj = mapper.build(root, project_id, rules_classifier=rules.extract_rules)
    app_map = app_map_obj.to_dict()
    invariants = rules.invariants_from_rules(app_map_obj)
    findings_list = findings_store.list_findings(project_id)
    findings = {str(item["id"]): item for item in findings_list}
    prior = [item for item in findings_list if item.get("status") in ("confirmed", "likely")]

    analysis_row, changeset = _latest_changeset(project_id)
    impact, tokens, impact_notes = _impact(project_id, root, app_map, changeset, analysis_row,
                                           findings_list)
    changed = set(_changed_paths(changeset))
    knowledge = _knowledge(project_id, changeset)
    index = _knowledge_index(knowledge)
    regression = _regression_selection(project_id, changeset, impact_only)

    missions = mission_generator.generate(
        project_id, invariants, app_map_obj.features, mode=mode,
        regression_findings=prior, max_missions=max(limit, 10), ops=None, spec_doc=None)

    selected: list[tuple[Any, list[str], list[dict], float]] = []
    excluded: list[dict[str, Any]] = []
    for mission in missions:
        reasons = _impact_reasons(mission, findings=findings, changed=changed, tokens=tokens)
        if impact_only and not reasons:
            excluded.append({"mission_id": mission.mission_id, "agent": mission.agent,
                             "title": mission.title,
                             "reason": "no changed path or impacted surface matches this mission"})
            continue
        knowledge_items, weight = _knowledge_for(mission, index)
        selected.append((mission, reasons, knowledge_items, weight))
    selected.sort(key=lambda entry: (-entry[3], entry[0].priority, entry[0].title,
                                     entry[0].mission_id))
    truncated = max(0, len(selected) - limit)
    selected = selected[:limit]

    actions: list[dict[str, Any]] = []
    regression_action = _regression_action(project_id, regression, target=clean, mode=mode,
                                           knowledge=knowledge, config=config)
    if regression_action is not None:
        actions.append(regression_action)
    for mission, reasons, knowledge_items, weight in selected:
        actions.append(_mission_action(
            mission, target=clean, mode=mode, reasons=reasons, knowledge_items=knowledge_items,
            knowledge_weight=weight, findings=findings, config=config))

    estimated = {"steps": 0, "requests": 0, "sessions": 0}
    for action in actions:
        for key in estimated:
            estimated[key] += int((action.get("cost") or {}).get(key) or 0)
    approval_actions = [action["action_id"] for action in actions if action["requires_approval"]]
    executable = bool(clean)
    document: dict[str, Any] = {
        "schema": SCHEMA,
        "project_id": project_id,
        "repository": str(root),
        "created_at": _now(),
        "mode": mode,
        "target": clean,
        "impact_only": impact_only,
        "command": "qa",
        "executable": executable,
        "executable_reason": (None if executable else
                              "no target was recorded; rebuild the plan with --target before "
                              "executing it"),
        "llm_usage": "none",
        "inputs": {
            "changeset": None if changeset is None else {
                "kind": changeset.get("kind"),
                "base": changeset.get("base"),
                "head": changeset.get("head"),
                "changed_paths": sorted(changed),
                "summary": changeset.get("summary") or {},
            },
            "impact": impact,
            "knowledge": knowledge,
            "regression": regression,
            "notes": impact_notes,
        },
        "actions": actions,
        "summary": {
            "actions": len(actions),
            "missions": sum(1 for action in actions if action["kind"] == "mission"),
            "regression_tests": sum(len(action.get("selected_tests") or [])
                                    for action in actions),
            "estimated": estimated,
            "requires_approval": bool(approval_actions),
            "approval_actions": approval_actions,
            "excluded_missions": excluded[:50],
            "truncated_missions": truncated,
        },
        "wording": list(_WORDING),
    }
    if impact_only and not changeset:
        document["inputs"]["notes"].append(
            "an impact-only plan requires a saved change set; no action can be derived from an "
            "absent change set")
    if not actions and impact_only:
        document["inputs"]["notes"].append(
            "the change set reached no mapped mission, regression test or impacted surface; an "
            "empty plan is reported instead of an invented action")
    document = redact_obj(document)
    digest = digest_of(document)
    plan_id = f"PL-{_stamp()}-{digest[:8]}"
    document["digest"] = digest
    document["plan_id"] = plan_id
    document["status"] = "draft"
    document["approval"] = {"required": bool(approval_actions), "approved_at": None,
                            "approved_by": None}
    document["artifact"] = _artifact_relative(project_id, plan_id)
    return document


def _artifact_relative(project_id: str, plan_id: str) -> str:
    folder = paths.project_state_dir(project_id)
    return str((folder / f"plan-{paths.slug_segment(plan_id)}.json")
               .relative_to(paths.kh_root()))


def _write(project_id: str, document: dict[str, Any]) -> Path:
    folder = paths.project_state_dir(project_id)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"plan-{paths.slug_segment(document['plan_id'])}.json"
    target.write_text(
        json.dumps(redact_obj(document), indent=2, sort_keys=True, default=str, ensure_ascii=False),
        encoding="utf-8")
    target.chmod(0o600)
    return target


def save(document: dict[str, Any]) -> dict[str, Any]:
    """Persist the artifact and its derived rows. Approval is never reset by a re-record."""
    project_id = document["project_id"]
    digest = digest_of(document)
    if digest != document.get("digest"):
        raise PlannerError("the plan digest does not match its content; refusing to store it")
    existing = store.get_qa_plan(document["plan_id"])
    if existing and existing.get("approved_at") and existing.get("digest") == digest:
        document["approval"] = {
            "required": bool(document["summary"]["requires_approval"]),
            "approved_at": existing["approved_at"], "approved_by": existing["approved_by"]}
        document["status"] = existing["status"]
    document["artifact"] = _artifact_relative(project_id, document["plan_id"])
    target = _write(project_id, document)
    record = dict(document)
    record["artifact"] = str(target.relative_to(paths.kh_root()))
    return store.record_qa_plan(record)


def load(plan_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    row = store.get_qa_plan(plan_id)
    if not row:
        raise PlannerError(f"plan '{plan_id}' was not found")
    artifact = row.get("artifact")
    if not artifact:
        raise PlannerError(f"plan '{plan_id}' has no artifact path")
    target = Path(str(artifact))
    if not target.is_absolute():
        target = paths.kh_root() / target
    try:
        resolved = target.resolve()
        resolved.relative_to(paths.kh_root().resolve())
        document = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PlannerError(f"plan '{plan_id}' could not be read: {type(exc).__name__}: {exc}") from exc
    if not isinstance(document, dict):
        raise PlannerError(f"plan '{plan_id}' is not a JSON object")
    computed = digest_of(document)
    if (document.get("digest") != computed or row.get("digest") != computed
            or document.get("plan_id") != plan_id):
        raise PlannerError(
            f"plan drifted: '{plan_id}' no longer matches its approved digest. Review the plan "
            f"again; the changed document will not be executed.")
    return document, row


def approve(plan_id: str, *, approved_by: str = "operator") -> dict[str, Any]:
    document, row = load(plan_id)
    if row.get("status") == "executing":
        raise PlannerError(f"plan '{plan_id}' is executing; it cannot be approved mid-run")
    stamp = _now()
    document["approval"] = {"required": bool(document["summary"]["requires_approval"]),
                            "approved_at": stamp, "approved_by": approved_by}
    document["status"] = "approved"
    _write(document["project_id"], document)
    updated = store.update_qa_plan(plan_id, status="approved", approved_at=stamp,
                                   approved_by=approved_by)
    if updated is None:
        raise PlannerError(f"plan '{plan_id}' disappeared while approving it")
    return document


def mark(plan_id: str, status: str, *, run_id: str | None = None) -> dict[str, Any] | None:
    """Record a lifecycle transition in the database and the artifact."""
    row = store.get_qa_plan(plan_id)
    if row is None:
        return None
    try:
        document, _ = load(plan_id)
    except PlannerError:
        document = None
    if document is not None:
        document["status"] = status
        _write(document["project_id"], document)
    return store.update_qa_plan(plan_id, status=status, **({"run_id": run_id} if run_id else {}))


def load_for_execution(plan_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    document, row = load(plan_id)
    if row.get("status") == "executing":
        raise PlannerError(f"plan '{plan_id}' is already executing")
    if row.get("status") in ("completed", "failed", "cancelled", "budget_exceeded"):
        raise PlannerError(f"plan '{plan_id}' already ran (status '{row.get('status')}'); build a "
                           f"new plan instead of reusing an old one")
    if not document.get("executable"):
        raise PlannerError(document.get("executable_reason") or "this plan is not executable")
    return document, row


def plan_missions(document: dict[str, Any]) -> list[dict[str, Any]]:
    return [action["mission"] for action in document.get("actions") or []
            if action.get("kind") == "mission" and action.get("mission")]


def plan_regression(document: dict[str, Any]) -> dict[str, Any] | None:
    for action in document.get("actions") or []:
        if action.get("kind") == "regression":
            return {"action_id": action.get("action_id"),
                    "selected_tests": action.get("selected_tests") or [],
                    "target": action.get("target")}
    return None


def register_project(root: Path) -> str:
    """Register a detected project if the caller has not done so yet; returns the id."""
    profile = detector.detect(root)
    if not profile.get("valid_project"):
        raise PlannerError(profile.get("reason") or "not a project directory")
    detector.write_profile(profile)
    return profile["project_id"]
