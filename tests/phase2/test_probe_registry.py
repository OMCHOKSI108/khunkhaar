"""The probe registry is the contract between planning, execution and verification."""

from __future__ import annotations

from core.agents import probes


DOCUMENTED_KINDS = {
    "authorization", "validation", "workflow", "idempotency", "concurrency",
    "timezone", "atomicity", "responsive",
}


def test_every_documented_invariant_kind_has_an_executable_probe():
    for kind in DOCUMENTED_KINDS:
        assert probes.PROBE_BY_KIND.get(kind) is not None, kind


def test_standalone_probes_are_also_verifiable():
    for kind, probe_cls in probes.STANDALONE_PROBES.items():
        assert probes.PROBE_BY_KIND.get(kind) is not None, kind
        assert issubclass(probe_cls, probes._RuleProbe)


def test_probe_classes_declare_category_severity_agent_and_kind():
    for kind, probe_cls in probes.PROBE_BY_KIND.items():
        if probe_cls is None:
            continue
        assert probe_cls.kind, kind
        assert probe_cls.category in (
            "functional", "security", "ux", "accessibility", "api", "database",
            "performance", "concurrency", "business_logic", "reliability"), kind
        assert probe_cls.severity in ("critical", "high", "medium", "low", "informational")
        assert probe_cls.agent in (
            "user_simulator", "workflow_breaker", "edge_hunter", "security_hunter",
            "ux_inspector", "api_hunter", "db_hunter", "regression_intel"), kind