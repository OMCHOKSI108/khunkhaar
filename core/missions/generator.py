"""Mission generation.

Missions are derived from the application's own documented rules and its mapped surface. The
generator never encodes a defect: it encodes *how to look* (an invariant kind maps to a probe,
a mapped feature maps to a journey). Execution decides what is true.
"""

from __future__ import annotations

from typing import Any

from core.agents.probes import PROBE_BY_KIND
from core.missions.models import Mission

AGENT_BY_KIND = {
    "authorization": "security_hunter",
    "validation": "api_hunter",
    "workflow": "workflow_breaker",
    "idempotency": "workflow_breaker",
    "concurrency": "db_hunter",
    "timezone": "edge_hunter",
    "atomicity": "db_hunter",
    "responsive": "ux_inspector",
    "ux": "ux_inspector",
}

PRIORITY_BY_KIND = {
    "authorization": 1, "workflow": 1, "atomicity": 1, "concurrency": 2,
    "idempotency": 2, "validation": 2, "timezone": 3, "responsive": 3, "ux": 4,
}


def from_invariants(project_id: str, invariants: list[dict[str, Any]],
                    limit: int = 40) -> list[Mission]:
    missions: list[Mission] = []
    for invariant in invariants:
        kind = invariant.get("kind") or "other"
        if kind not in PROBE_BY_KIND or PROBE_BY_KIND[kind] is None:
            continue
        agent = AGENT_BY_KIND.get(kind, "api_hunter")
        missions.append(Mission(
            agent=agent,
            title=invariant.get("description") or f"{kind} invariant",
            kind=f"invariant:{kind}",
            project_id=project_id,
            description=invariant.get("method") or "",
            category={
                "authorization": "security", "validation": "api", "workflow": "business_logic",
                "idempotency": "reliability", "concurrency": "concurrency", "timezone": "business_logic",
                "atomicity": "database", "responsive": "ux", "ux": "accessibility",
            }.get(kind, "functional"),
            severity={"authorization": "high", "workflow": "high", "atomicity": "high",
                      "concurrency": "high", "idempotency": "high"}.get(kind, "medium"),
            invariant_id=invariant.get("id"),
            priority=PRIORITY_BY_KIND.get(kind, 4),
            mode_required="adversarial" if kind in ("concurrency", "idempotency") else "controlled",
            payload={"invariant": invariant, "probe_kind": kind},
        ))
        if len(missions) >= limit:
            break
    return sorted(missions, key=lambda item: (item.priority, item.title))


def standard_missions(project_id: str) -> list[Mission]:
    """Checks that do not need a documented rule, but still need execution."""
    return [
        Mission(agent="ux_inspector", title="Landing page accessibility scan",
                kind="standalone:accessibility", project_id=project_id, category="accessibility",
                severity="low", priority=5, mode_required="controlled",
                payload={"probe_kind": "standalone"}),
        Mission(agent="ux_inspector", title="Link health and console errors",
                kind="standalone:links", project_id=project_id, category="functional",
                severity="low", priority=5, mode_required="controlled",
                payload={"probe_kind": "standalone"}),
        Mission(agent="security_hunter", title="Unauthenticated access to private endpoints",
                kind="standalone:auth_boundary", project_id=project_id, category="security",
                severity="high", priority=2, mode_required="controlled",
                payload={"probe_kind": "auth_boundary"}),
    ]


def exploration_mission(project_id: str, max_steps: int = 24) -> Mission:
    return Mission(agent="user_simulator", title="Bounded exploratory browser crawl",
                   kind="exploration", project_id=project_id, category="functional",
                   severity="low", priority=4, mode_required="controlled",
                   description="Observe, decide, act, verify and record each screen state.",
                   payload={"max_steps": max_steps})


def journey_mission(project_id: str, feature: str) -> Mission:
    return Mission(agent="workflow_breaker", title=f"Mutated workflow journeys ({feature})",
                   kind="journeys", project_id=project_id, category="business_logic",
                   severity="high", priority=2, mode_required="adversarial",
                   description="Replay the happy path and controlled mutations of its steps.",
                   payload={"feature": feature})


def _journey_features(features: list[str], ops: list[dict[str, Any]] | None,
                      spec_doc: dict[str, Any] | None) -> list[str]:
    """Only features with a buildable create-then-act journey deserve a journey mission."""
    if ops is None:
        return features
    from core.journeys import models as journeys

    viable = []
    for feature in features:
        if journeys.build_journey(ops, spec_doc, feature) is not None:
            viable.append(feature)
    return viable


def regression_missions(project_id: str, findings: list[dict[str, Any]],
                        limit: int = 10) -> list[Mission]:
    missions: list[Mission] = []
    for finding in findings[:limit]:
        if finding.get("status") not in ("confirmed", "likely"):
            continue
        kind = (finding.get("reproduction") or {}).get("probe_kind")
        if kind not in PROBE_BY_KIND or PROBE_BY_KIND[kind] is None:
            continue
        invariant = (finding.get("reproduction") or {}).get("invariant") or {
            "description": finding.get("title"), "kind": kind,
            "feature": finding.get("feature"), "id": finding.get("invariant_id"),
            "rule_id": None,
        }
        missions.append(Mission(
            agent="regression_intel", title=f"Regression: {finding.get('title')}",
            kind=f"regression:{kind}", project_id=project_id, category=finding.get("category"),
            severity=finding.get("severity"), priority=1, mode_required="controlled",
            payload={"invariant": invariant, "probe_kind": kind,
                     "finding_id": finding.get("id") or finding.get("finding_id")},
        ))
    return missions


def generate(project_id: str, invariants: list[dict[str, Any]],
             features: list[str] | None = None, *, mode: str = "controlled",
             regression_findings: list[dict[str, Any]] | None = None,
             max_missions: int = 60, ops: list[dict[str, Any]] | None = None,
             spec_doc: dict[str, Any] | None = None) -> list[Mission]:
    missions: list[Mission] = []
    missions.extend(from_invariants(project_id, invariants))
    missions.extend(standard_missions(project_id))
    if mode != "observe":
        missions.append(exploration_mission(project_id))
    unique_features = [f for f in (features or []) if f not in ("api", "static", "docs", "login")]
    if mode == "adversarial":
        for feature in _journey_features(unique_features, ops, spec_doc)[:2]:
            missions.append(journey_mission(project_id, feature))
    if regression_findings:
        missions.extend(regression_missions(project_id, regression_findings))
    # one mission per invariant kind is useful; duplicates of the same probe on the same feature
    # only burn budget, so keep the highest-priority mission for each (kind, feature) pair.
    seen: set[tuple] = set()
    deduped: list[Mission] = []
    for mission in sorted(missions, key=lambda item: item.priority):
        feature = (mission.payload.get("invariant") or {}).get("feature", "")
        if mission.kind.startswith("invariant:"):
            key = (mission.payload.get("probe_kind") or mission.kind, feature)
        elif mission.kind.startswith("regression:"):
            # Regression replays stay separate from invariant probes: they are the only check
            # when a rule has disappeared from the documentation since the defect was confirmed.
            key = ("regression", mission.payload.get("probe_kind") or mission.kind, feature)
        else:
            key = (mission.kind, mission.mission_id)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(mission)
        if len(deduped) >= max_missions:
            break
    return deduped
