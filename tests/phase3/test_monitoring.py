"""P6: monitoring schedules, honest due/missed semantics, checks and notifications.

The scheduled dispatch is exercised with fixed clocks, so a simulated two-hour downtime is real
arithmetic against stored windows rather than a mocked answer. A smoke check talks to the local
mock application; regression execution is replaced by a recording fake, because these tests
assert how monitoring classifies and reports results, not how a browser test is driven.
"""

from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from core import cli
from core.findings import store as findings_store
from core.metadata import store
from core.monitoring import checks, cron, runner, schedules
from core.notify import notifier
from core.runtime.budget import Budget
from core.shared import paths

import p4_helpers

MOCK_TARGET = "http://127.0.0.1:8731"
BASE = datetime(2026, 3, 1, 0, 5, tzinfo=timezone.utc)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "monitor-app"
    (root / "app").mkdir(parents=True)
    (root / "app" / "__init__.py").write_text("", encoding="utf-8")
    (root / "app" / "main.py").write_text(
        "from fastapi import FastAPI\n\napp = FastAPI()\n\n\n@app.get('/items')\n"
        "def items():\n    return []\n", encoding="utf-8")
    (root / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        "[project]\nname = \"monitor-fixture\"\nversion = \"0.0.0\"\n", encoding="utf-8")
    return root


@pytest.fixture
def project(kh_root, repo):
    return p4_helpers.register(repo)


@pytest.fixture(autouse=True)
def quiet_channels(kh_root, patch_config):
    """No test should pop a real desktop notification; individual tests re-enable it explicitly."""
    patch_config("notifications", {"terminal": True, "desktop": {"enabled": False}})


@pytest.fixture
def monitoring_on(patch_config):
    patch_config("monitoring", {"enabled": True})
    return True


def make_schedule(project_id: str, *, expression: str = "0 * * * *", target: str = MOCK_TARGET,
                  checks_list: tuple[str, ...] = ("smoke",), now: datetime = BASE,
                  name: str = "default") -> dict:
    schedule, _ = schedules.create_or_update(
        project_id, name, expression, mode="controlled", target=target,
        checks=list(checks_list), now=now, tz=timezone.utc)
    return schedule


def budget() -> Budget:
    return Budget(max_steps=10, max_requests=10, max_browser_sessions=0, max_seconds=20)


# ------------------------------------------------------------------------------- cron


def test_cron_aliases_names_and_steps():
    daily = cron.CronExpression.parse("@daily")
    assert daily.next_after(datetime(2026, 3, 1, 10, 30, tzinfo=timezone.utc)) == \
        datetime(2026, 3, 2, 0, 0, tzinfo=timezone.utc)
    named = cron.CronExpression.parse("*/15 9-17 * jan,mar mon-fri")
    assert named.minute.values == frozenset({0, 15, 30, 45})
    assert named.hour.values == frozenset(range(9, 18))
    assert named.month.values == frozenset({1, 3})
    assert named.dow.values == frozenset({1, 2, 3, 4, 5})


def test_cron_sunday_is_both_zero_and_seven():
    seven = cron.CronExpression.parse("0 12 * * 7")
    zero = cron.CronExpression.parse("0 12 * * 0")
    moment = datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc)  # a Sunday
    assert seven.next_after(moment) == zero.next_after(moment) == \
        datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


def test_cron_restricted_day_of_month_or_day_of_week():
    expression = cron.CronExpression.parse("0 0 1 * mon")
    found = expression.occurrences(datetime(2026, 2, 28, 23, 59, tzinfo=timezone.utc),
                                   datetime(2026, 3, 31, 23, 59, tzinfo=timezone.utc))
    days = [item.day for item in found]
    assert days == [1, 2, 9, 16, 23, 30]


@pytest.mark.parametrize("expression", [
    "", "not a cron", "61 * * * *", "0 24 * * *", "* * 0 * *", "10-5 * * * *",
    "*/0 * * * *", "0 0 30 2 *", "0 0 * *", "0 0 * * 8", "0 0 * * mon-xyz",
])
def test_cron_refuses_malformed_or_impossible(expression):
    with pytest.raises(cron.CronError):
        cron.CronExpression.parse(expression)


# --------------------------------------------------------------------------- schedules


def test_set_writes_a_0600_artifact_that_matches_the_store(project):
    schedule = make_schedule(project, expression="0 3 * * *")
    artifact = schedules.artifact_path(schedule["schedule_id"])
    assert artifact.is_file()
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    payload, problem = schedules.read_artifact(schedule)
    assert problem is None
    assert payload["expression"] == "0 3 * * *"
    assert payload["checks"] == ["smoke"]
    assert schedule["next_due_at"] == "2026-03-01T03:00:00+00:00"
    assert "KHUNKHAAR installs no cron entry" in payload["note"]


def test_re_setting_preserves_users_file_and_db_path(project):
    schedule, _ = schedules.create_or_update(
        project, "seeded", "0 * * * *", target=MOCK_TARGET, checks=["regression"],
        users_file="/tmp/users.json", db_path="/tmp/app.sqlite", now=BASE, tz=timezone.utc)
    updated, created = schedules.create_or_update(
        project, "seeded", "0 * * * *", target=MOCK_TARGET, checks=["regression"],
        now=BASE + timedelta(minutes=1), tz=timezone.utc)
    assert created is False
    payload, _ = schedules.read_artifact(updated)
    assert payload["users_file"] == "/tmp/users.json"
    assert payload["db_path"] == "/tmp/app.sqlite"
    assert schedule["schedule_id"] == updated["schedule_id"]


@pytest.mark.parametrize("kwargs, message", [
    ({"mode": "adversarial"}, "mode must be one of"),
    ({"checks": ["shell"]}, "unknown check"),
    ({"checks": []}, "at least one check"),
    ({"target": None}, "needs a target"),
    ({"expression": "0 0 30 2 *"}, "no occurrence"),
])
def test_set_refuses_invalid_definitions(project, kwargs, message):
    values = {"expression": "0 * * * *", "mode": "controlled", "target": MOCK_TARGET,
              "checks": ["smoke"], "now": BASE, "tz": timezone.utc, **kwargs}
    with pytest.raises((schedules.ScheduleError, cron.CronError), match=message):
        schedules.create_or_update(project, "bad", **values)


def test_set_refuses_an_unregistered_project(kh_root, tmp_path):
    store.init()
    root = tmp_path / "stranger"
    root.mkdir()
    with pytest.raises(schedules.ScheduleError, match="not registered"):
        schedules.create_or_update("P-missing", "x", "0 * * * *", target=MOCK_TARGET,
                                   checks=["smoke"], now=BASE, tz=timezone.utc)


def test_an_edited_artifact_is_refused_rather_than_obeyed(project):
    schedule = make_schedule(project)
    path = schedules.artifact_path(schedule["schedule_id"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["checks"] = ["smoke", "regression"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    loaded, problem = schedules.read_artifact(schedule)
    assert loaded is None
    assert "disagrees with the store" in problem


def test_pause_never_creates_a_missed_run_and_resume_recalculates(project, monitoring_on):
    schedule = make_schedule(project)
    paused = schedules.set_enabled(schedule["schedule_id"], False, now=BASE)
    assert paused["enabled"] is False and paused["paused_at"]
    state = schedules.state(paused, BASE + timedelta(days=2))
    assert state["state"] == "paused"

    result = runner.run_due(now=BASE + timedelta(days=2))
    assert result["schedules"][0]["status"] == "paused"
    assert store.list_monitoring_runs(project) == []

    resumed = schedules.set_enabled(schedule["schedule_id"], True,
                                    now=BASE + timedelta(days=2))
    assert resumed["enabled"] is True
    assert schedules.parse_stamp(resumed["next_due_at"]) > BASE + timedelta(days=2)


# ------------------------------------------------------------------------------- runner


def test_disabled_monitoring_dispatches_and_records_nothing(project):
    make_schedule(project)
    result = runner.run_due(now=BASE + timedelta(hours=2))
    assert result["enabled"] is False
    assert result["status"] == "disabled"
    assert store.list_monitoring_runs(project) == []


def test_due_window_executes_and_records_start_and_finish(project, monitoring_on, mock_server):
    schedule = make_schedule(project)
    moment = BASE + timedelta(minutes=55)  # 01:00 is due
    result = runner.run_due(now=moment)
    item = result["schedules"][0]
    assert item["executed"] is True, item
    assert item["status"] == "passed", item
    assert item["checks"][0]["check"] == "smoke"

    rows = store.list_monitoring_runs(project)
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "passed"
    assert row["started_at"] and row["finished_at"]
    assert row["due_at"] == "2026-03-01T01:00:00+00:00"
    evidence_path = row["evidence"]["artifact"]
    assert evidence_path
    assert json.loads((paths.kh_root() / evidence_path).read_text())["run_id"]

    updated = store.get_monitoring_schedule(schedule["schedule_id"])
    assert updated["next_due_at"] == "2026-03-01T02:00:00+00:00"
    again = runner.run_due(now=moment + timedelta(minutes=1))
    assert again["executed"] == 0
    assert len(store.list_monitoring_runs(project)) == 1


def test_downtime_windows_are_missed_not_claimed_as_runs(project, monitoring_on):
    schedule = make_schedule(project)
    result = runner.run_due(now=BASE + timedelta(hours=4, minutes=20))  # 01-04 due
    item = result["schedules"][0]
    assert item["executed"] is False
    assert item["status"] == "missed"
    assert item["missed"] == 4
    assert result["status"] == "missed"

    rows = store.list_monitoring_runs(project)
    assert len(rows) == 4
    assert {row["status"] for row in rows} == {"missed"}
    assert all(row["started_at"] is None and row["finished_at"] is None for row in rows)
    assert {row["due_at"] for row in rows} == {
        "2026-03-01T01:00:00+00:00", "2026-03-01T02:00:00+00:00",
        "2026-03-01T03:00:00+00:00", "2026-03-01T04:00:00+00:00"}

    updated = store.get_monitoring_schedule(schedule["schedule_id"])
    assert updated["next_due_at"] == "2026-03-01T05:00:00+00:00"
    alerts = store.list_alerts(project)
    assert [alert["kind"] for alert in alerts] == ["monitoring_missed"]
    assert alerts[0]["detail"]["count"] == 4


def test_a_window_within_grace_runs_only_the_latest(project, monitoring_on, mock_server):
    make_schedule(project)
    result = runner.run_due(now=BASE + timedelta(hours=1, minutes=10))
    item = result["schedules"][0]
    assert item["executed"] is True
    assert item["missed"] == 0
    rows = store.list_monitoring_runs(project)
    assert len(rows) == 1 and rows[0]["status"] == "passed"
    assert rows[0]["due_at"] == "2026-03-01T01:00:00+00:00"


def test_a_crash_after_recording_does_not_replay_or_call_it_missed(project, monitoring_on):
    schedule = make_schedule(project)
    store.record_monitoring_run({
        "schedule_id": schedule["schedule_id"], "project_id": project,
        "due_at": "2026-03-01T01:00:00+00:00", "started_at": "2026-03-01T01:00:01+00:00",
        "finished_at": "2026-03-01T01:00:03+00:00", "status": "passed",
        "note": "recorded by the previous process", "evidence": {}})
    result = runner.run_due(now=BASE + timedelta(hours=1, minutes=10))
    item = result["schedules"][0]
    assert item["executed"] is False
    assert item["status"] == "idle"
    assert item["missed"] == 0
    assert "already has a recorded" in item["note"]
    assert len(store.list_monitoring_runs(project)) == 1
    updated = store.get_monitoring_schedule(schedule["schedule_id"])
    assert updated["next_due_at"] == "2026-03-01T02:00:00+00:00"


def test_catch_up_executes_only_the_latest_window_within_the_limit(project, monitoring_on,
                                                                  patch_config, mock_server):
    patch_config("monitoring", {"catch_up": True, "max_overdue_before_missed": 2})
    make_schedule(project)
    result = runner.run_due(now=BASE + timedelta(hours=2, minutes=20))  # 01 and 02 due
    item = result["schedules"][0]
    assert item["executed"] is True
    assert item["missed"] == 1
    rows = store.list_monitoring_runs(project)
    assert sorted(row["status"] for row in rows) == ["missed", "passed"]
    assert [row["due_at"] for row in rows if row["status"] == "passed"] == \
        ["2026-03-01T02:00:00+00:00"]


def test_stale_windows_beyond_the_catch_up_limit_stay_missed(project, monitoring_on,
                                                             patch_config):
    patch_config("monitoring", {"catch_up": True, "max_overdue_before_missed": 2})
    make_schedule(project)
    result = runner.run_due(now=BASE + timedelta(hours=4, minutes=20))
    item = result["schedules"][0]
    assert item["executed"] is False
    assert item["status"] == "missed"
    assert item["missed"] == 4


def test_a_non_loopback_target_needs_an_authorization_grant(project, monitoring_on,
                                                            patch_config, monkeypatch):
    patch_config("api", {"allowed_hosts": ["127.0.0.1:8731", "10.20.30.40:8080"]})
    make_schedule(project, target="http://10.20.30.40:8080")

    def must_not_run(*args, **kwargs):
        raise AssertionError("a check ran before the outbound preflight")

    monkeypatch.setattr(checks, "run_checks", must_not_run)
    result = runner.run_due(now=BASE + timedelta(hours=1))
    item = result["schedules"][0]
    assert item["executed"] is True
    assert item["status"] == "environment_unavailable"
    assert "no unexpired authorization grant" in item["note"]
    row = store.list_monitoring_runs(project)[0]
    assert row["status"] == "environment_unavailable"
    assert [alert["kind"] for alert in store.list_alerts(project)] == ["environment_unavailable"]


def test_an_authorized_non_loopback_target_can_run(project, monitoring_on, patch_config,
                                                   monkeypatch):
    patch_config("api", {"allowed_hosts": ["127.0.0.1:8731", "10.20.30.40:8080"]})
    make_schedule(project, target="http://10.20.30.40:8080")
    store.grant_authorization(project, "http://10.20.30.40:8080", "observe",
                              granted_by="operator")
    seen: list[str] = []

    def fake_checks(project_id, schedule, artifact, *, target, budget, config):
        seen.append(target)
        return {"checks": [{"check": "smoke", "status": "passed", "alerts": []}],
                "status": "passed", "alerts": []}

    monkeypatch.setattr(checks, "run_checks", fake_checks)
    result = runner.run_due(now=BASE + timedelta(hours=1))
    assert result["schedules"][0]["status"] == "passed"
    assert seen == ["http://10.20.30.40:8080"]


def test_a_drifted_schedule_is_misconfigured_and_never_executes(project, monitoring_on,
                                                               monkeypatch):
    schedule = make_schedule(project)
    path = schedules.artifact_path(schedule["schedule_id"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["target"] = "http://127.0.0.1:9999"
    path.write_text(json.dumps(payload), encoding="utf-8")

    called = []
    monkeypatch.setattr(checks, "run_checks", lambda *a, **k: called.append(True))
    result = runner.run_due(now=BASE + timedelta(hours=1))
    item = result["schedules"][0]
    assert item["status"] == "misconfigured"
    assert called == []
    assert store.list_monitoring_runs(project) == []
    assert [alert["kind"] for alert in store.list_alerts(project)] == ["monitoring_blocked"]


# ------------------------------------------------------------------------------- checks


def test_smoke_passes_against_the_mock_application(project, mock_server):
    result = checks.run_checks(project, {"checks": ["smoke"]}, {}, target=MOCK_TARGET,
                               budget=budget(), config={"check_timeout_seconds": 10})
    assert result["status"] == "passed"
    assert result["checks"][0]["http_status"] == 200
    assert findings_store.list_findings(project) == []


def test_smoke_transport_error_is_never_a_defect(project, patch_config):
    patch_config("api", {"allowed_hosts": ["127.0.0.1:38731"]})
    result = checks.run_checks(project, {"checks": ["smoke"]}, {},
                               target="http://127.0.0.1:38731", budget=budget(),
                               config={"check_timeout_seconds": 5})
    assert result["status"] == "environment_unavailable"
    assert [alert["kind"] for alert in store.list_alerts(project)] == \
        ["environment_unavailable"]
    assert findings_store.list_findings(project) == []


def test_a_5xx_smoke_is_a_failure_but_not_a_finding(project, monkeypatch):
    class Response:
        status = 503
        error = None
        error_kind = None

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def get(self, path):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(checks, "ApiClient", Client)
    result = checks.run_checks(project, {"checks": ["smoke"]}, {},
                               target=MOCK_TARGET, budget=budget(), config={})
    assert result["status"] == "failed"
    assert [alert["kind"] for alert in store.list_alerts(project)] == ["smoke_failed"]
    assert findings_store.list_findings(project) == []


def _fake_selection(finding_id: str) -> dict:
    return {"selected": [{"test": {"test_id": "RT-1", "finding_id": finding_id,
                                   "reliability": "validated"}, "reason": "fixture"}],
            "excluded": [], "changed_paths": []}


def test_regression_fail_alerts_but_monitoring_never_creates_a_finding(project, monkeypatch):
    finding = p4_helpers.add_finding(project)
    before = len(findings_store.list_findings(project))
    monkeypatch.setattr(checks.runner, "select_all", lambda project_id: _fake_selection(finding["id"]))
    monkeypatch.setattr(checks.runner, "execute", lambda *a, **k: {
        "run_id": "run-fixture", "target_host": "127.0.0.1",
        "summary": {"by_result": {"fail": 1}},
        "results": [{"result": "fail", "test_id": "RT-1", "finding_id": finding["id"],
                     "evidence": "evidence/regression/run-fixture/RT-1.json"}]})
    result = checks.run_checks(project, {"checks": ["regression"]}, {}, target=MOCK_TARGET,
                               budget=budget(), config={})
    assert result["status"] == "failed"
    alerts = store.list_alerts(project)
    assert [alert["kind"] for alert in alerts] == ["regression_failed"]
    assert alerts[0]["finding_id"] == finding["id"]
    assert len(findings_store.list_findings(project)) == before


def test_regression_environment_failure_is_not_reported_as_a_defect(project, monkeypatch):
    finding = p4_helpers.add_finding(project)
    monkeypatch.setattr(checks.runner, "select_all", lambda project_id: _fake_selection(finding["id"]))
    monkeypatch.setattr(checks.runner, "execute", lambda *a, **k: {
        "run_id": "run-fixture", "target_host": "127.0.0.1",
        "summary": {"by_result": {"environment_unavailable": 1}},
        "results": [{"result": "environment_unavailable", "test_id": "RT-1",
                     "finding_id": finding["id"], "evidence": None}]})
    result = checks.run_checks(project, {"checks": ["regression"]}, {}, target=MOCK_TARGET,
                               budget=budget(), config={})
    assert result["status"] == "environment_unavailable"
    assert [alert["kind"] for alert in store.list_alerts(project)] == \
        ["environment_unavailable"]


def test_regression_with_no_tests_is_skipped_not_passed(project):
    result = checks.run_checks(project, {"checks": ["regression"]}, {}, target=MOCK_TARGET,
                               budget=budget(), config={})
    assert result["status"] == "skipped"
    assert "nothing was verified" in result["checks"][0]["note"]


def test_uncovered_invariants_are_reported_and_nothing_is_claimed_verified(project):
    findings_store.add_invariant(project, {"id": "INV-1", "rule_id": "R1",
                                           "description": "a user cannot read another user's item",
                                           "source": "docs/requirements.md", "feature": "items",
                                           "confidence": 0.8})
    result = checks.run_checks(project, {"checks": ["invariants"]}, {}, target=MOCK_TARGET,
                               budget=budget(), config={})
    assert result["status"] == "skipped"
    assert result["checks"][0]["uncovered"] == 1
    assert [alert["kind"] for alert in store.list_alerts(project)] == ["invariant_uncovered"]


# --------------------------------------------------------------------------- notifier


def test_alert_is_recorded_and_printed_structured(project, capsys):
    alert = notifier.notify(project, "smoke_failed", "high", "smoke check failed",
                            detail={"check": "smoke", "http_status": 503,
                                    "request_body": "must never be stored"})
    output = capsys.readouterr().err
    assert "KHUNKHAAR ALERT " in output
    payload = json.loads(output.split("KHUNKHAAR ALERT ", 1)[1])
    assert payload["kind"] == "smoke_failed"
    assert "request_body" not in json.dumps(payload)
    assert "request_body" not in json.dumps(alert["detail"])
    stored = store.list_alerts(project)[0]
    assert stored["status"] == "delivered"
    assert stored["detail"]["deliveries"][0]["channel"] == "terminal"


def test_absent_notify_send_is_a_recorded_warning_not_a_crash(project, patch_config,
                                                              monkeypatch):
    patch_config("notifications", {"desktop": {"enabled": True, "timeout_seconds": 5}})
    monkeypatch.setattr(notifier.shutil, "which", lambda name: None)
    alert = notifier.notify(project, "monitoring_missed", "low", "a window was missed")
    desktop = [item for item in alert["detail"]["deliveries"] if item["channel"] == "desktop"]
    assert desktop[0]["status"] == "unavailable"
    assert "notify-send" in desktop[0]["reason"]


def test_desktop_delivery_uses_a_fixed_argument_list(project, patch_config, monkeypatch):
    patch_config("notifications", {"desktop": {"enabled": True, "timeout_seconds": 5}})
    monkeypatch.setattr(notifier.shutil, "which", lambda name: "/usr/bin/notify-send")
    seen: list[list[str]] = []

    class Completed:
        returncode = 0

    def fake_run(argv, **kwargs):
        seen.append(list(argv))
        assert kwargs.get("shell") is not True
        return Completed()

    monkeypatch.setattr(notifier.subprocess, "run", fake_run)
    notifier.notify(project, "smoke_failed", "high", "smoke check failed")
    assert seen == [["/usr/bin/notify-send", "--app-name=KHUNKHAAR QA", "--urgency=critical",
                     "KHUNKHAAR QA: high smoke_failed", "smoke check failed"]]


def test_external_channels_are_never_sent(project, patch_config):
    patch_config("notifications", {"email": {"enabled": True, "smtp_host": "smtp.example",
                                             "recipients": ["qa@example.invalid"]},
                                   "webhook": {"enabled": True,
                                               "url": "https://hooks.example.invalid/x"}})
    alert = notifier.notify(project, "monitoring_blocked", "medium", "nothing was executed")
    remote = [item for item in alert["detail"]["deliveries"]
              if item["channel"] in ("email", "webhook")]
    assert {item["status"] for item in remote} == {"not_sent"}
    assert all("not implemented" in item["reason"] for item in remote)


def test_unknown_alert_kind_or_severity_is_refused(project):
    with pytest.raises(notifier.AlertError):
        notifier.notify(project, "defect_found", "high", "x")
    with pytest.raises(notifier.AlertError):
        notifier.notify(project, "smoke_failed", "urgent", "x")


SECRETS = st.sampled_from([
    "ghp_" + "a" * 36,
    "AKIA" + "ABCDEFGHIJKLMNOP",
    "eyJ" + "a" * 30 + "." + "b" * 20 + "." + "c" * 20,
    "postgresql://user:hunter2@db.example.invalid/app",
    "xoxb-1234567890-abcdefghijkl",
])


@settings(max_examples=40, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(secret=SECRETS, where=st.sampled_from(["message", "title", "feature", "evidence"]))
def test_notification_payload_never_carries_a_secret_value(project, capsys, secret, where):
    detail = {"title": "safe title", "feature": "service", "evidence": "evidence/x.json"}
    message = "a monitoring check failed"
    if where == "message":
        message = f"a check failed with {secret}"
    else:
        detail[where] = f"prefix {secret}"
    alert = notifier.notify(project, "smoke_failed", "high", message, detail=detail)
    captured = capsys.readouterr()
    combined = captured.out + captured.err + json.dumps(alert, default=str)
    stored = json.dumps(store.list_alerts(project), default=str)
    assert secret not in combined
    assert secret not in stored


# -------------------------------------------------------------------------------- CLI


def run_cli(*argv: str) -> int:
    return cli.main(["khunkhaar", *argv])


def test_cli_set_requires_monitoring_to_be_enabled(project, capsys):
    rc = run_cli("monitor", "set", "--project", project, "--schedule", "0 * * * *",
                 "--target", MOCK_TARGET, "--checks", "smoke")
    assert rc == 2
    assert "monitoring is disabled" in capsys.readouterr().out


def test_cli_set_refuses_an_external_target_without_authorization(project, monitoring_on,
                                                                  patch_config, capsys):
    patch_config("api", {"allowed_hosts": ["127.0.0.1:8731", "10.20.30.40:8080"]})
    rc = run_cli("monitor", "set", "--project", project, "--schedule", "0 * * * *",
                 "--target", "http://10.20.30.40:8080", "--checks", "smoke")
    assert rc == 2
    assert "no unexpired authorization grant" in capsys.readouterr().out
    assert store.list_monitoring_schedules(project) == []


def test_cli_set_list_status_doctor_and_pause(project, monitoring_on, capsys, mock_server):
    rc = run_cli("monitor", "set", "--project", project, "--schedule", "*/5 * * * *",
                 "--target", MOCK_TARGET, "--checks", "smoke", "--json")
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    schedule_id = payload["schedule"]["schedule_id"]
    assert payload["installed"] is False

    assert run_cli("monitor", "list", "--project", project, "--json") == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["count"] == 1 and listed["schedules"][0]["schedule_id"] == schedule_id

    assert run_cli("monitor", "status", "--schedule", schedule_id, "--json") == 0
    state = json.loads(capsys.readouterr().out)
    assert state["schedules"][0]["state"] in ("scheduled", "due", "missed")

    assert run_cli("monitor", "doctor", "--schedule", schedule_id) == 0
    doctor = capsys.readouterr().out
    assert "KHUNKHAAR installed nothing" in doctor
    assert "monitor run-due" in doctor and "systemd-run" in doctor

    assert run_cli("monitor", "pause", "--schedule", schedule_id) == 0
    paused = store.get_monitoring_schedule(schedule_id)
    assert paused["enabled"] is False and paused["paused_at"]
    assert run_cli("monitor", "resume", "--schedule", schedule_id) == 0
    resumed = store.get_monitoring_schedule(schedule_id)
    assert resumed["enabled"] is True and resumed["paused_at"] is None


def test_cli_run_due_marks_a_stale_window_missed_and_exits_three(project, monitoring_on,
                                                                 patch_config, monkeypatch,
                                                                 capsys):
    fixed = datetime(2026, 3, 1, 6, 7, tzinfo=timezone.utc)
    monkeypatch.setattr(runner.schedules, "utcnow", lambda: fixed)
    patch_config("monitoring", {"schedule_grace_seconds": 0})
    rc = run_cli("monitor", "set", "--project", project, "--schedule", "0 * * * *",
                 "--target", MOCK_TARGET, "--checks", "smoke", "--json")
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    schedule_id = payload["schedule"]["schedule_id"]
    schedule = store.get_monitoring_schedule(schedule_id)
    artifact, _ = schedules.read_artifact(schedule)
    zone = timezone(timedelta(seconds=int(artifact["timezone_offset_seconds"])),
                    artifact["timezone_name"])
    current_hour = fixed.astimezone(zone).replace(minute=0, second=0, microsecond=0)
    store.update_monitoring_schedule(schedule_id,
                                     next_due_at=schedules.stamp(current_hour - timedelta(hours=4)))
    schedules.refresh_artifact(store.get_monitoring_schedule(schedule_id))

    assert run_cli("monitor", "run-due", "--schedule", schedule_id, "--json") == 3
    missed = json.loads(capsys.readouterr().out)
    assert missed["schedules"][0]["status"] == "missed"
    assert missed["schedules"][0]["missed"] == 5
    assert missed["schedules"][0]["executed"] is False

    assert run_cli("monitor", "history", "--project", project, "--json") == 0
    history = json.loads(capsys.readouterr().out)
    assert history["count"] == 5
    assert {row["status"] for row in history["runs"]} == {"missed"}
