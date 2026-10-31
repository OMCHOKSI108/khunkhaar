"""Phase 2 finding identity, enumerations and verification status rules."""

from __future__ import annotations

import pytest

from core.findings import models


def _finding(**overrides):
    base = dict(project_id="proj", feature="requests", category="security",
                title="A user may only read their own requests",
                description="d", severity="high")
    base.update(overrides)
    return models.Finding(**base)


def test_fingerprint_is_stable_and_normalised():
    assert models.fingerprint("p", "security", "Requests", "A  User May READ") == \
        models.fingerprint("p", "security", "requests", "a user may read")
    assert models.fingerprint("p", "security", "requests", "a") != \
        models.fingerprint("p", "security", "requests", "b")


def test_finding_identity_is_derived_from_fingerprint():
    finding = _finding()
    assert finding.fingerprint
    assert finding.finding_id == f"F-{finding.fingerprint[:10]}"


def test_unknown_enumerations_are_rejected():
    with pytest.raises(ValueError):
        _finding(category="nonsense")
    with pytest.raises(ValueError):
        _finding(severity="blocker")
    with pytest.raises(ValueError):
        _finding(status="definitely")


def test_only_verifier_statuses_leave_unverified():
    assert "unverified" not in models.VERIFIER_STATUSES
    assert {"confirmed", "likely", "not_reproducible", "false_positive",
            "needs_human"} <= models.VERIFIER_STATUSES


def test_confidence_follows_status():
    assert models.CONFIDENCE_BY_STATUS["confirmed"] > models.CONFIDENCE_BY_STATUS["likely"]
    assert models.CONFIDENCE_BY_STATUS["likely"] > models.CONFIDENCE_BY_STATUS["unverified"]
    assert models.CONFIDENCE_BY_STATUS["false_positive"] < \
        models.CONFIDENCE_BY_STATUS["not_reproducible"]


def test_serialisation_contains_reproduction_and_invariant():
    finding = _finding(invariant_id="rule-1", reproduction={"type": "probe"})
    payload = finding.to_dict()
    assert payload["invariant_id"] == "rule-1"
    assert payload["reproduction"] == {"type": "probe"}
    assert payload["status"] == "unverified"
