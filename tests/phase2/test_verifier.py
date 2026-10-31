"""Independent verification classification rules.

The verifier is exercised with stubbed reproductions so the decision table is tested without a
live target; the real end-to-end reproductions are recorded in the Phase 2 validation report.
"""

from __future__ import annotations

import asyncio

from core.verification import verifier as verifier_module


def _finding(reproduction=None):
    return {"id": "F-test", "reproduction": reproduction or {},
            "status": "unverified", "reproduction_json": None}


def _verifier(monkeypatch):
    instance = verifier_module.Verifier(
        project_id="p", target="http://127.0.0.1:1", base_url="http://127.0.0.1:1",
        app_map={}, spec_doc=None, ops=[], users={}, db=None, budget=None, log=None)
    monkeypatch.setattr(instance, "_save", lambda finding_id, payload: "evidence/fake.json")
    calls = {}
    monkeypatch.setattr(verifier_module.findings_store, "update_status",
                        lambda *args, **kwargs: calls.setdefault("status", args[1]))
    return instance, calls


def _outcomes(sequence):
    iterator = iter(sequence)

    async def fake_attempt(reproduction):
        return next(iterator)

    return fake_attempt


def _run(instance, finding):
    return asyncio.run(instance.verify(finding))


def test_two_reproductions_confirm(monkeypatch):
    instance, calls = _verifier(monkeypatch)
    monkeypatch.setattr(instance, "_attempt",
                        _outcomes([{"violated": True}, {"violated": True}]))
    outcome = _run(instance, _finding({"type": "probe", "probe_kind": "authorization"}))
    assert outcome.status == "confirmed"
    assert outcome.reproductions == 2
    assert calls["status"] == "confirmed"


def test_one_reproduction_is_only_likely(monkeypatch):
    instance, _ = _verifier(monkeypatch)
    monkeypatch.setattr(instance, "_attempt",
                        _outcomes([{"violated": True}, {"violated": False}]))
    outcome = _run(instance, _finding({"type": "probe", "probe_kind": "authorization"}))
    assert outcome.status == "likely"


def test_zero_reproductions_are_rejected(monkeypatch):
    instance, _ = _verifier(monkeypatch)
    monkeypatch.setattr(instance, "_attempt",
                        _outcomes([{"violated": False}, {"violated": False}]))
    outcome = _run(instance, _finding({"type": "probe", "probe_kind": "authorization"}))
    assert outcome.status == "not_reproducible"
    assert outcome.reproductions == 0


def test_infrastructure_failure_never_confirms(monkeypatch):
    instance, _ = _verifier(monkeypatch)
    error = {"violated": True, "infrastructure_error": "no probe available"}
    monkeypatch.setattr(instance, "_attempt", _outcomes([error, error]))
    outcome = _run(instance, _finding({"type": "probe", "probe_kind": "authorization"}))
    assert outcome.status == "unverified"


def test_missing_reproduction_escalates_to_human(monkeypatch):
    instance, calls = _verifier(monkeypatch)
    outcome = _run(instance, _finding({}))
    assert outcome.status == "needs_human"
    assert calls["status"] == "needs_human"