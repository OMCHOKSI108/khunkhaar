"""Mission planning: invariant mapping, mode gating, dedupe and journey feasibility."""

from __future__ import annotations

from core.missions import models, generator


INVARIANTS = [
    {"id": "i-auth", "rule_id": "1", "kind": "authorization", "feature": "requests",
     "description": "A user may only read their own requests"},
    {"id": "i-wf", "rule_id": "3", "kind": "workflow", "feature": "request",
     "description": "A request may only be approved after it is submitted"},
    {"id": "i-conc", "rule_id": "5", "kind": "concurrency", "feature": "clock",
     "description": "Only one open clock-in per user"},
]


def _ops():
    return [
        {"method": "POST", "path": "/api/requests",
         "request_schema": {"properties": {"title": {"type": "string"}}},
         "summary": "", "operation_id": None, "params": [], "response_types": ["application/json"]},
        {"method": "POST", "path": "/api/requests/{request_id}/submit",
         "request_schema": {}, "summary": "", "operation_id": None, "params": [],
         "response_types": ["application/json"]},
        {"method": "POST", "path": "/api/requests/{request_id}/approve",
         "request_schema": {}, "summary": "", "operation_id": None, "params": [],
         "response_types": ["application/json"]},
        {"method": "POST", "path": "/api/clock/in", "request_schema": {"properties": {}},
         "summary": "", "operation_id": None, "params": [], "response_types": ["application/json"]},
    ]


def test_invariants_map_to_the_right_specialist_and_mode():
    missions = generator.from_invariants("p", INVARIANTS)
    by_kind = {mission.payload["probe_kind"]: mission for mission in missions}
    assert by_kind["authorization"].agent == "security_hunter"
    assert by_kind["authorization"].mode_required == "controlled"
    assert by_kind["workflow"].agent == "workflow_breaker"
    assert by_kind["concurrency"].agent == "db_hunter"
    assert by_kind["concurrency"].mode_required == "adversarial"


def test_missions_cannot_require_an_unknown_mode():
    mission = generator.from_invariants("p", INVARIANTS)[0]
    assert models.MODES[mission.mode_required] in models.MODES.values()
    assert mission.allowed_in("adversarial") is True


def test_duplicate_probe_and_feature_are_deduped():
    duplicated = [INVARIANTS[0], dict(INVARIANTS[0], id="i-auth-2", rule_id="9")]
    missions = generator.generate("p", duplicated, mode="controlled")
    auth = [m for m in missions if m.payload.get("probe_kind") == "authorization"]
    assert len(auth) == 1


def test_journey_missions_only_target_buildable_features():
    missions = generator.generate("p", INVARIANTS, ["clock", "requests"], mode="adversarial",
                                  ops=_ops())
    journeys = [m for m in missions if m.kind == "journeys"]
    assert [m.payload["feature"] for m in journeys] == ["requests"]


def test_regression_replays_are_not_deduped_against_invariant_probes():
    finding = {
        "id": "F-1", "status": "confirmed", "title": "A user may only read their own requests",
        "feature": "requests", "category": "security", "severity": "high",
        "reproduction": {"probe_kind": "authorization", "invariant": INVARIANTS[0]},
    }
    missions = generator.generate("p", [INVARIANTS[0]], mode="controlled",
                                  regression_findings=[finding])
    kinds = [mission.kind for mission in missions]
    assert "invariant:authorization" in kinds
    assert "regression:authorization" in kinds


def test_observe_mode_plans_no_exploration():
    missions = generator.generate("p", [], [], mode="observe")
    assert not [m for m in missions if m.kind == "exploration"]
