"""Module D: generation, validation, execution history and impact selection.

The tests never start a target. Generated modules are read as artifacts, and execution is
exercised through the real runner/validator with only the subprocess boundary replaced, so the
store writes, evidence permissions and verdict rules are the production ones.
"""

from __future__ import annotations

import json
import stat

from core.findings import store as findings_store
from core.metadata import store
from core.regression import derive, generate, process, runner, select, validate

import p4_helpers


def _generate(pid: str, finding: dict) -> dict:
    return generate.generate(finding, project_id=pid)


def test_confirmed_finding_generates_a_runnable_module(kh_root, impact_repo):
    pid = p4_helpers.register(impact_repo.root)
    finding = p4_helpers.add_finding(pid, title="Invalid values are accepted")
    result = _generate(pid, finding)

    path = kh_root / result["path"]
    assert path.is_file()
    source = path.read_text(encoding="utf-8")
    assert "runtime.execute" in source
    assert "KHUNKHAAR_TARGET" in source
    assert "KHUNKHAAR_REGRESSION_INVERTED" in source
    assert result["test"]["test_id"].lower().replace("-", "_") in source
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((kh_root / result["intent_path"]).stat().st_mode) == 0o600

    intent = json.loads((kh_root / result["intent_path"]).read_text(encoding="utf-8"))
    assert intent["method"] == "probe"
    assert intent["reproduction"]["probe_kind"] == "validation"
    assert intent["finding_id"] == finding["id"]
    assert result["test"]["reliability"] == "generated"
    assert store.get_regression_test(result["test"]["test_id"])["finding_id"] == finding["id"]


def test_likely_and_unverified_findings_are_excluded_with_the_reason(kh_root, impact_repo):
    pid = p4_helpers.register(impact_repo.root)
    confirmed = p4_helpers.add_finding(pid, title="Confirmed defect")
    likely = p4_helpers.add_finding(pid, title="Likely defect", status="likely")
    unverified = p4_helpers.add_finding(pid, title="Raw observation", status="unverified")

    selection = select.select(pid)
    assert [item["id"] for item in selection["eligible"]] == [confirmed["id"]]
    reasons = {item["finding_id"]: item["reason"] for item in selection["excluded"]}
    assert likely["id"] in reasons and "only an independently confirmed finding" in reasons[
        likely["id"]]
    assert unverified["id"] in reasons

    generated = generate.generate_for_findings(pid, selection["eligible"])
    assert len(generated) == 1
    assert store.list_regression_tests(pid)[0]["finding_id"] == confirmed["id"]


def test_a_confirmed_journey_replays_the_captured_transcript(kh_root, impact_repo):
    pid = p4_helpers.register(impact_repo.root)
    _, verification_ref = p4_helpers.write_journey_transcript(kh_root)
    finding = p4_helpers.add_finding(pid, title="Approval can be skipped", status="unverified",
                                     reproduction=p4_helpers.journey_reproduction())
    findings_store.update_status(finding["id"], "confirmed", actor="verifier",
                                 note="reproduced in the browser journey",
                                 evidence={"verification": verification_ref})
    finding = findings_store.get_finding(finding["id"])

    intent = derive.derive(finding, project_id=pid)
    assert intent["method"] == "journey"
    assert intent["captured"] is True
    assert intent["reproduction"]["journey"]["name"] == "approve an item"
    assert intent["reproduction"]["mutation"]["name"] == "skip-approval"

    result = _generate(pid, finding)
    source = (kh_root / result["path"]).read_text(encoding="utf-8")
    assert "approve an item" in source
    assert "skip-approval" in source


def test_regeneration_is_deterministic_and_does_not_reset_history(kh_root, impact_repo):
    pid = p4_helpers.register(impact_repo.root)
    finding = p4_helpers.add_finding(pid, title="Duplicate submission creates two records")
    first = _generate(pid, finding)
    first_bytes = (kh_root / first["path"]).read_bytes()
    test_id = first["test"]["test_id"]

    second = _generate(pid, finding)
    assert second["test"]["test_id"] == test_id
    assert second["changed"] is False
    assert (kh_root / second["path"]).read_bytes() == first_bytes

    store.update_regression_test(test_id, reliability="trusted", confidence=0.97,
                                 validated_path="regression/validated/example.py")
    third = _generate(pid, finding)
    assert third["changed"] is False
    assert third["test"]["reliability"] == "trusted"
    assert third["test"]["confidence"] == 0.97
    assert third["test"]["validated_path"] == "regression/validated/example.py"


def test_validation_promotes_only_after_every_violation_and_an_inverted_pass(
        kh_root, impact_repo, monkeypatch):
    pid = p4_helpers.register(impact_repo.root)
    finding = p4_helpers.add_finding(pid, title="Invalid values are accepted")
    test = _generate(pid, finding)["test"]
    calls: list[bool] = []

    def fake(path, *, target, users_path=None, db_path=None, inverted=False, timeout=120.0):
        calls.append(inverted)
        return p4_helpers.fake_run(violated=True, rc=1 if not inverted else 0)

    monkeypatch.setattr(process, "run_test", fake)
    reports = validate.validate(pid, [test], target="http://127.0.0.1:8740", runs=2)

    assert len(calls) == 3
    assert calls == [False, False, True]
    report = reports[0]
    assert report["verdict"] == "trusted"
    assert "every validation run" in report["note"]
    updated = store.get_regression_test(test["test_id"])
    assert updated["reliability"] == "trusted"
    assert updated["confidence"] >= 0.9
    assert updated["validated_path"]
    validated_file = kh_root / updated["validated_path"]
    assert validated_file.is_file()
    assert stat.S_IMODE(validated_file.stat().st_mode) == 0o600
    assert stat.S_IMODE((kh_root / report["artifact"]).stat().st_mode) == 0o600


def test_environment_unavailable_is_never_a_promotion_or_a_defect(kh_root, impact_repo,
                                                                  monkeypatch):
    pid = p4_helpers.register(impact_repo.root)
    finding = p4_helpers.add_finding(pid, title="Validation target is down")
    test = _generate(pid, finding)["test"]

    monkeypatch.setattr(
        process, "run_test",
        lambda *args, **kwargs: p4_helpers.fake_run(
            violated=False, rc=0, infrastructure_error="connection refused"))
    report = validate.validate(pid, [test], target="http://127.0.0.1:8740", runs=1)[0]

    assert report["verdict"] == "environment_unavailable"
    assert "no conclusion" in report["note"]
    updated = store.get_regression_test(test["test_id"])
    assert updated["reliability"] == "generated"
    assert updated["validated_path"] is None
    assert updated["last_failure_at"] is None


def test_not_reproduced_is_not_trusted(kh_root, impact_repo, monkeypatch):
    pid = p4_helpers.register(impact_repo.root)
    finding = p4_helpers.add_finding(pid, title="No longer reproduces")
    test = _generate(pid, finding)["test"]

    monkeypatch.setattr(process, "run_test",
                        lambda *args, **kwargs: p4_helpers.fake_run(violated=False, rc=0))
    report = validate.validate(pid, [test], target="http://127.0.0.1:8740", runs=1)[0]

    assert report["verdict"] == "not_reproduced"
    assert store.get_regression_test(test["test_id"])["reliability"] == "generated"


def test_runner_records_environment_unavailable_without_a_failure_or_confidence_loss(
        kh_root, impact_repo, monkeypatch):
    pid = p4_helpers.register(impact_repo.root)
    finding = p4_helpers.add_finding(pid, title="Runner target is down")
    test = _generate(pid, finding)["test"]
    before = store.get_regression_test(test["test_id"])["confidence"]

    monkeypatch.setattr(
        runner.process, "run_test",
        lambda *args, **kwargs: p4_helpers.fake_run(
            violated=False, rc=0, infrastructure_error="name or service not known"))
    execution = runner.execute(pid, runner.select_all(pid), target="http://127.0.0.1:8740")

    assert execution["summary"]["by_result"] == {"environment_unavailable": 1}
    updated = store.get_regression_test(test["test_id"])
    assert updated["last_failure_at"] is None
    assert updated["confidence"] == before
    row = store.list_regression_runs(pid, test["test_id"], limit=1)[0]
    assert row["result"] == "environment_unavailable"
    assert row["note"].startswith("environment unavailable:")
    assert stat.S_IMODE((kh_root / row["evidence"]).stat().st_mode) == 0o600


def test_execution_history_survives_regeneration(kh_root, impact_repo, monkeypatch):
    pid = p4_helpers.register(impact_repo.root)
    finding = p4_helpers.add_finding(pid, title="Repeated submission creates two records")
    test = _generate(pid, finding)["test"]

    monkeypatch.setattr(runner.process, "run_test",
                        lambda *args, **kwargs: p4_helpers.fake_run(violated=False, rc=0))
    runner.execute(pid, runner.select_all(pid), target="http://127.0.0.1:8740")
    runner.execute(pid, runner.select_all(pid), target="http://127.0.0.1:8740")
    runs = store.list_regression_runs(pid, test["test_id"], limit=10)
    assert len(runs) == 2
    confidence = store.get_regression_test(test["test_id"])["confidence"]
    assert confidence > test["confidence"]
    assert store.get_regression_test(test["test_id"])["last_success_at"]

    regenerated = _generate(pid, finding)
    assert regenerated["changed"] is False
    assert len(store.list_regression_runs(pid, test["test_id"], limit=10)) == 2
    assert store.get_regression_test(test["test_id"])["confidence"] == confidence
    assert store.get_regression_test(test["test_id"])["last_success_at"]


def test_impact_selection_returns_the_linked_test_and_explains_the_others(
        kh_root, impact_repo):
    pid = p4_helpers.register(impact_repo.root)
    linked = p4_helpers.add_finding(pid, title="Service rejects an empty name",
                                    source_refs=("app/service.py:24",))
    unrelated = p4_helpers.add_finding(pid, title="Unrelated page typo", category="ux",
                                       source_refs=("web/page.ts:8",))
    linked_test = _generate(pid, linked)["test"]
    unrelated_test = _generate(pid, unrelated)["test"]

    selection = runner.select_by_impact(pid, ["app/service.py"])
    selected_ids = [entry["test"]["test_id"] for entry in selection["selected"]]
    assert selected_ids == [linked_test["test_id"]]
    excluded = {entry["test_id"]: entry["reason"] for entry in selection["excluded"]}
    assert unrelated_test["test_id"] in excluded
    assert "references no changed path" in excluded[unrelated_test["test_id"]]
    assert selection["changed_paths"] == ["app/service.py"]


def test_quarantined_tests_are_not_run(kh_root, impact_repo, monkeypatch):
    pid = p4_helpers.register(impact_repo.root)
    finding = p4_helpers.add_finding(pid, title="Unstable generated test")
    test = _generate(pid, finding)["test"]
    store.update_regression_test(test["test_id"], reliability="quarantined")

    selection = runner.select_all(pid)
    assert selection["selected"] == []
    assert any(item["test_id"] == test["test_id"] for item in selection["excluded"])

    def explode(*args, **kwargs):
        raise AssertionError("a quarantined test must not be executed")

    monkeypatch.setattr(runner.process, "run_test", explode)
    execution = runner.execute(pid, selection, target="http://127.0.0.1:8740")
    assert execution["summary"]["by_result"] == {}
