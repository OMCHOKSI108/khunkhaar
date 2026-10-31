"""P7: the private local dashboard.

Every page is rendered from rows written through the real store, not from mocks. The tests prove
the loopback bind, the token gate and its constant-time comparison, the read-only boundary, the
local-only assets and the 390 px layout with the same overflow measurement the Phase 2 responsive
probe uses.
"""

from __future__ import annotations

import ast
import base64
import json
import socket
import stat
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import p4_helpers
from core.findings import store as findings_store
from core.metadata import store
from dashboard import auth, data
from dashboard.app import create_app

TOKEN = "dashboard-test-token-" + "x" * 24
SECRET = "ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
RUN = "phase2-20261030-120000"


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "vulnapp"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname = 'vulnapp'\n", encoding="utf-8")
    (root / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    return root


class Seed:
    def __init__(self, **values):
        self.__dict__.update(values)


@pytest.fixture
def seeded(kh_root, tmp_path):
    repo = _repo(tmp_path)
    project_id = p4_helpers.register(repo)
    store.init()
    findings_store.init()

    profile_path = Path(store.get_project(project_id)["profile_path"])
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["git"] = {"branch": "main", "commit": "abc1234", "is_repo": True}
    profile_path.write_text(json.dumps(profile), encoding="utf-8")

    security = p4_helpers.add_finding(
        project_id, title="A user may only read their own requests", category="security",
        severity="high", status="confirmed")
    false_positive = p4_helpers.add_finding(
        project_id, title="A false alarm from the first pass", category="api",
        status="false_positive")
    open_finding = p4_helpers.add_finding(
        project_id, title="An unverified finding", category="ux", status="unverified")
    xss = p4_helpers.add_finding(
        project_id, title="<script>alert(1)</script>", category="ux", status="likely")

    store.start_run(RUN, project_id, "phase2:all")
    store.finish_run(RUN, "completed", note="22 mission(s)")

    evidence_dir = kh_root / "evidence" / "dashboard"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "screenshot.png").write_bytes(PNG)
    store.add_evidence_ref(project_id, RUN, "screenshot", "evidence/dashboard/screenshot.png",
                           len(PNG))
    (evidence_dir / "probe.json").write_text(
        json.dumps({"authorization": f"Bearer {SECRET}", "password": "hunter2"}),
        encoding="utf-8")
    store.add_evidence_ref(project_id, RUN, "json", "evidence/dashboard/probe.json", 64)
    (evidence_dir / "report.html").write_text(
        "<html><script>alert('evidence')</script><body>evidence</body></html>", encoding="utf-8")
    store.add_evidence_ref(project_id, RUN, "report", "evidence/dashboard/report.html", 64)
    (evidence_dir / "verification.json").write_text(
        json.dumps({"status": "confirmed", "raw_body": {"password": "hunter2"}}), encoding="utf-8")
    store.add_evidence_ref(project_id, RUN, "verification",
                           "evidence/dashboard/verification.json", 32)

    report = {
        "run_id": RUN,
        "agents": {"api_hunter": {"missions": 2, "issues": 1, "infrastructure_errors": 0,
                                  "notes": [f"raw note with {SECRET}"]},
                   "ux_inspector": {"missions": 1, "issues": 0, "infrastructure_errors": 1}},
        "missions": {"planned": 3, "executed": 2, "skipped": 1, "failed": 0},
        "budget": {"steps": 10, "max_steps": 100, "requests": 12, "max_requests": 100,
                   "elapsed_seconds": 1.5},
        "errors": [{"message": "raw body {'password': 'hunter2'}"}],
        "request_body": {"password": "hunter2"},
        "verification": [{"finding_id": security["id"], "status": "confirmed", "attempts": 2,
                          "reproductions": 2,
                          "evidence": "evidence/dashboard/verification.json",
                          "notes": [f"raw verification note {SECRET}"]}],
    }
    reports_dir = kh_root / "reports" / "phase2" / project_id
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / f"{RUN}-run.json").write_text(json.dumps(report), encoding="utf-8")
    store.add_report_ref(project_id, RUN, f"reports/phase2/{project_id}/{RUN}-run.json")

    changeset = {
        "kind": "working-tree", "base": "main", "head": "HEAD", "base_source": "candidate",
        "summary": {"commits": 1, "files_changed": 1, "insertions": 3, "deletions": 1,
                    "renames": 0, "removed": 0},
        "files": [{"path": "app/service.py", "status": "modified", "insertions": 3,
                   "deletions": 1, "functions": ["ItemService.rename"],
                   "attribution": "python-ast", "hunks": 1}],
        "notes": [],
    }
    git_dir = kh_root / "reports" / "git"
    git_dir.mkdir(parents=True, exist_ok=True)
    (git_dir / "analysis.json").write_text(json.dumps({"changeset": changeset}),
                                           encoding="utf-8")
    store.record_git_analysis(project_id, RUN, "working-tree", base_ref="main", head_ref="HEAD",
                              summary=changeset["summary"], artifact="reports/git/analysis.json")

    impact_artifact = {
        "impact": {
            "impacted": [{"type": "class", "name": "ItemService", "path": "app/service.py",
                          "via": "imports", "depth": 1}],
            "surface": [{"type": "route", "name": "POST /items", "via": "handler"}],
            "tests": [{"name": "test_create", "path": "tests/test_service.py"}],
            "workflows": [{"name": "create item", "feature": "items"}],
            "summary": {"impacted": 1, "surface": 1, "depth": 1},
        },
        "risk": {"score": 42.5, "factors": [{"name": "change complexity", "weight": 0.2,
                                             "contribution": 8.0}]},
    }
    (evidence_dir / "impact.json").write_text(json.dumps(impact_artifact), encoding="utf-8")
    analysis = store.latest_git_analysis(project_id)
    store.record_impact_report(
        project_id, analysis_id=analysis["id"], base_ref="main", head_ref="HEAD",
        risk_score=42.5, risk_factors=[{"name": "change complexity", "weight": 0.2,
                                       "contribution": 8.0}],
        artifact="evidence/dashboard/impact.json")
    store.replace_impact_graph(
        project_id,
        [{"node_id": "route:POST /items", "type": "route", "name": "POST /items",
          "path": "app/main.py"},
         {"node_id": "class:ItemService", "type": "class", "name": "ItemService",
          "path": "app/service.py"}],
        [{"src": "route:POST /items", "dst": "class:ItemService", "relation": "calls"}])
    store.record_qa_plan({
        "plan_id": "PLAN-DASH1", "project_id": project_id, "mode": "controlled",
        "digest": "a" * 64, "status": "approved", "approved_by": "operator",
        "approved_at": "2026-10-31T00:00:00+00:00", "summary": {"actions": 1},
        "actions": [{"agent": "api_hunter", "kind": "mission", "feature": "items",
                     "requires_approval": True,
                     "cost": {"requests": 5, "steps": 5, "sessions": 0},
                     "selected_tests": ["RT-1"]}],
    })
    store.upsert_regression_test({
        "test_id": "RT-1", "project_id": project_id, "finding_id": security["id"],
        "category": "security", "method": "pytest", "objective": "another user is refused",
        "reliability": "generated", "confidence": 0.4,
    })
    store.record_regression_run(
        project_id, test_id="RT-1", result="pass", run_id=RUN, base_ref="main", head_ref="HEAD",
        duration_ms=12, note="expected refusal observed", evidence=None)

    refs = {row["path"]: row["id"] for row in store.list_evidence_refs(project_id=project_id)}
    return Seed(project_id=project_id, repo=repo, security=security, false_positive=false_positive,
                open_finding=open_finding, xss=xss, refs=refs)


@pytest.fixture
def app(seeded):
    return create_app(token=TOKEN)


@pytest.fixture
def client(app):
    browser = TestClient(app)
    response = browser.post("/login", data={"token": TOKEN}, follow_redirects=False)
    assert response.status_code == 303
    return browser


# ---------------------------------------------------------------- tokens and sessions


def test_token_is_created_once_with_0600(kh_root):
    first = auth.ensure_token()
    second = auth.ensure_token()
    assert first == second
    assert len(first) >= 32
    assert stat.S_IMODE(auth.token_path().stat().st_mode) == 0o600
    assert stat.S_IMODE(auth.token_path().parent.stat().st_mode) == 0o700


def test_a_symlinked_token_is_refused(kh_root):
    auth.token_path().parent.mkdir(parents=True, exist_ok=True)
    elsewhere = kh_root / "elsewhere"
    elsewhere.write_text("x" * 40, encoding="utf-8")
    auth.token_path().symlink_to(elsewhere)
    with pytest.raises(auth.DashboardAuthError):
        auth.ensure_token()


def test_wrong_token_is_rejected_without_cookie(app):
    response = TestClient(app).post("/login", data={"token": "wrong"}, follow_redirects=False)
    assert response.status_code == 401
    assert "did not match" in response.text
    assert "set-cookie" not in response.headers


def test_session_cookie_is_strict_and_never_contains_the_token(app):
    response = TestClient(app).post("/login", data={"token": TOKEN}, follow_redirects=False)
    cookie = response.headers["set-cookie"]
    value = response.cookies.get(auth.COOKIE_NAME)
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "Max-Age=28800" in cookie
    assert TOKEN not in cookie
    assert value and value != TOKEN
    assert auth.valid_session(value, TOKEN)


def test_expired_and_forged_sessions_are_rejected():
    issued = int(time.time())
    cookie = auth.session_value("token", issued)
    assert auth.valid_session(cookie, "token", now=issued)
    assert auth.valid_session(cookie, "token", now=issued + auth.SESSION_TTL_SECONDS)
    assert not auth.valid_session(cookie, "token", now=issued + auth.SESSION_TTL_SECONDS + 1)
    assert not auth.valid_session(cookie, "token", now=issued - 1)
    assert not auth.valid_session(cookie, "another-token", now=issued)
    assert not auth.valid_session("garbage", "token", now=issued)


def test_token_comparison_goes_through_compare_digest(monkeypatch):
    calls = []
    real = auth.hmac.compare_digest

    def spy(left, right):
        calls.append((left, right))
        return real(left, right)

    monkeypatch.setattr(auth.hmac, "compare_digest", spy)
    assert auth.tokens_match("abc", "abc")
    assert not auth.tokens_match("abc", "abd")
    assert len(calls) == 2
    issued = int(time.time())
    assert auth.valid_session(auth.session_value("token", issued), "token", now=issued)
    assert len(calls) == 3


# ---------------------------------------------------------------- authentication boundary


@pytest.mark.parametrize("path", ["/", "/project/vulnapp-99adb8c2", "/execution/run-1",
                                  "/diff/vulnapp-99adb8c2", "/evidence/1"])
def test_unauthenticated_requests_are_rejected(app, path):
    response = TestClient(app).get(path)
    assert response.status_code == 401
    assert "/login" in response.text


def test_logout_requires_a_session(app):
    assert TestClient(app).post("/logout").status_code == 401


def test_login_page_is_the_only_public_route(app):
    anonymous = TestClient(app)
    assert anonymous.get("/login").status_code == 200
    response = anonymous.post("/login", data={"token": TOKEN}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert anonymous.get("/login", follow_redirects=False).status_code == 303


def test_security_headers_are_present_on_pages(client):
    response = client.get("/")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    policy = response.headers["content-security-policy"]
    assert "default-src 'none'" in policy
    assert "img-src 'self'" in policy


# ---------------------------------------------------------------- pages from real rows


def test_overview_renders_store_rows(client, seeded):
    response = client.get("/")
    assert response.status_code == 200
    html = response.text
    assert "vulnapp" in html
    assert RUN in html
    assert "api_hunter" in html
    assert "Confirmed defects" in html
    assert "environment-unavailable" in html
    assert "not a proven probability" in html
    assert SECRET not in html
    assert TOKEN not in html


def test_project_page_renders_rows_and_escapes_titles(client, seeded):
    response = client.get(f"/project/{seeded.project_id}")
    assert response.status_code == 200
    html = response.text
    assert "main" in html and "abc1234" in html
    assert "A user may only read their own requests" in html
    assert "RT-1" in html
    assert "ItemService" in html
    assert "42.5" in html
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert SECRET not in html


def test_execution_page_renders_agents_verification_and_evidence(client, seeded):
    response = client.get(f"/execution/{RUN}")
    assert response.status_code == 200
    html = response.text
    assert "api_hunter" in html and "ux_inspector" in html
    assert "2 / 3" in html
    assert "Errors recorded" in html
    assert "1" in html
    assert "verification.json" in html
    assert f"/evidence/{seeded.refs['evidence/dashboard/verification.json']}" in html
    assert f"/evidence/{seeded.refs['evidence/dashboard/screenshot.png']}" in html
    assert "hunter2" not in html
    assert SECRET not in html
    assert "raw verification note" not in html
    assert "raw note with" not in html


def test_diff_page_renders_the_analysis_impact_plan_and_outcome(client, seeded):
    response = client.get(f"/diff/{seeded.project_id}")
    assert response.status_code == 200
    html = response.text
    assert "app/service.py" in html
    assert "ItemService" in html
    assert "create item" in html
    assert "api_hunter" in html
    assert "Approximate risk 42.5 / 100" in html
    assert RUN in html
    assert "approved" in html


def test_missing_objects_render_honest_404s(client):
    assert client.get("/project/nope").status_code == 404
    assert client.get("/execution/nope").status_code == 404
    assert client.get("/diff/nope").status_code == 404
    assert "no recorded object" in client.get("/project/nope").text


def test_project_pages_stay_isolated(client, seeded, kh_root, tmp_path):
    other_repo = tmp_path / "other-app"
    other_repo.mkdir()
    (other_repo / "pyproject.toml").write_text("[project]\nname = 'other'\n", encoding="utf-8")
    other_id = p4_helpers.register(other_repo)
    p4_helpers.add_finding(other_id, title="A defect that belongs to the other project")
    response = client.get(f"/project/{seeded.project_id}")
    assert "A defect that belongs to the other project" not in response.text
    other = client.get(f"/project/{other_id}")
    assert "A defect that belongs to the other project" in other.text
    assert "A user may only read their own requests" not in other.text


# ---------------------------------------------------------------- evidence serving


def test_image_evidence_is_served_inline(client, seeded):
    ref_id = seeded.refs["evidence/dashboard/screenshot.png"]
    response = client.get(f"/evidence/{ref_id}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == PNG
    assert response.headers["x-content-type-options"] == "nosniff"


def test_text_evidence_is_redacted_plain_text(client, seeded):
    ref_id = seeded.refs["evidence/dashboard/probe.json"]
    response = client.get(f"/evidence/{ref_id}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert SECRET not in response.text
    assert "***REDACTED***" in response.text


def test_html_evidence_is_never_served_as_markup(client, seeded):
    ref_id = seeded.refs["evidence/dashboard/report.html"]
    response = client.get(f"/evidence/{ref_id}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "<script>" in response.text


def test_evidence_outside_the_allowed_roots_is_refused(client, seeded, kh_root):
    store.add_evidence_ref(seeded.project_id, RUN, "token", "data/profiles/dashboard/token", 10)
    row = [item for item in store.list_evidence_refs(project_id=seeded.project_id)
           if item["path"] == "data/profiles/dashboard/token"][0]
    store.add_evidence_ref(seeded.project_id, RUN, "outside", "/etc/passwd", 10)
    outside = [item for item in store.list_evidence_refs(project_id=seeded.project_id)
               if item["path"] == "/etc/passwd"][0]
    store.add_evidence_ref(seeded.project_id, RUN, "missing", "evidence/dashboard/missing.json", 1)
    missing = [item for item in store.list_evidence_refs(project_id=seeded.project_id)
               if item["path"] == "evidence/dashboard/missing.json"][0]
    assert client.get(f"/evidence/{row['id']}").status_code == 404
    assert client.get(f"/evidence/{outside['id']}").status_code == 404
    assert client.get(f"/evidence/{missing['id']}").status_code == 404
    assert client.get("/evidence/999999").status_code == 404


# ---------------------------------------------------------------- boundary: no execution


def test_every_page_is_read_only(client, seeded):
    pages = ["/", f"/project/{seeded.project_id}", f"/execution/{RUN}",
             f"/diff/{seeded.project_id}"]
    for page in pages:
        html = client.get(page).text
        assert "http://" not in html
        assert "https://" not in html
        assert "<script" not in html
    assert client.post(f"/project/{seeded.project_id}").status_code == 405
    assert client.post(f"/execution/{RUN}").status_code == 405
    assert client.post(f"/diff/{seeded.project_id}").status_code == 405


def test_no_dashboard_module_can_import_an_executor():
    forbidden = ("core.orchestrator", "core.commands", "core.execution", "core.agents",
                 "core.regression", "core.monitoring", "core.planning", "core.missions",
                 "subprocess")
    for path in Path(__file__).resolve().parents[2].joinpath("dashboard").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                assert not any(module == item or module.startswith(item + ".")
                               for item in forbidden), (path, module)


def test_route_table_has_no_execution_endpoint(app):
    expected = {
        "/login": {"GET", "POST"},
        "/logout": {"POST"},
        "/": {"GET"},
        "/project/{project_id}": {"GET"},
        "/execution/{run_id}": {"GET"},
        "/diff/{project_id}": {"GET"},
        "/evidence/{ref_id}": {"GET"},
    }
    actual: dict[str, set[str]] = {}
    for route in app.routes:
        actual.setdefault(route.path, set()).update(route.methods or [])
    assert actual == expected


def test_reading_pages_does_not_write_to_the_store(client, seeded):
    before_findings = len(findings_store.list_findings(seeded.project_id))
    before_refs = len(store.list_evidence_refs(project_id=seeded.project_id))
    before_report_refs = len(store.list_report_refs(project_id=seeded.project_id))
    before_files = {(path.relative_to(seeded.repo), path.stat().st_mtime_ns)
                    for path in seeded.repo.rglob("*") if path.is_file()}
    for page in ("/", f"/project/{seeded.project_id}", f"/execution/{RUN}",
                 f"/diff/{seeded.project_id}"):
        assert client.get(page).status_code == 200
    assert len(findings_store.list_findings(seeded.project_id)) == before_findings
    assert len(store.list_evidence_refs(project_id=seeded.project_id)) == before_refs
    assert len(store.list_report_refs(project_id=seeded.project_id)) == before_report_refs
    after_files = {(path.relative_to(seeded.repo), path.stat().st_mtime_ns)
                   for path in seeded.repo.rglob("*") if path.is_file()}
    assert after_files == before_files


# ---------------------------------------------------------------- CLI and bind policy


def test_default_bind_is_loopback_and_non_loopback_needs_the_flag(monkeypatch, kh_root, capsys):
    import uvicorn

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda application, **kwargs: calls.append(kwargs))
    from core.commands import dashboard as command

    assert command.run_start([]) == 0
    assert calls[-1]["host"] == "127.0.0.1"
    assert calls[-1]["port"] == 8899
    assert calls[-1]["access_log"] is False
    before = len(calls)
    assert command.run_start(["--host", "0.0.0.0"]) == 2
    assert len(calls) == before
    assert command.run_start(["--host", "0.0.0.0", "--allow-non-loopback"]) == 0
    assert calls[-1]["host"] == "0.0.0.0"
    stderr = capsys.readouterr().err
    assert "warning" in stderr
    assert "exposes the dashboard" in stderr


def test_cli_refuses_an_invalid_port(monkeypatch, kh_root):
    import uvicorn

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda application, **kwargs: calls.append(kwargs))
    from core.commands import dashboard as command

    assert command.run_start(["--port", "80"]) == 2
    assert command.run_start(["--port", "99999"]) == 2
    assert calls == []


def test_cli_prints_the_token_path_but_never_the_token(monkeypatch, kh_root, capsys):
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda application, **kwargs: None)
    from core.commands import dashboard as command

    assert command.run_start([]) == 0
    token = auth.ensure_token()
    output = capsys.readouterr().out
    assert token not in output
    assert str(auth.token_path()) in output
    assert "read-only" in output
    assert "cannot start a scan" in output


# ---------------------------------------------------------------- 390 px layout


@pytest.mark.browser
def test_pages_do_not_scroll_horizontally_at_390px(seeded, kh_root):
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import sync_playwright

    import uvicorn

    application = create_app(token=TOKEN)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(application, host="127.0.0.1", port=port,
                                          log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started, "the dashboard server did not start"

        overflow_script = """() => {
          const vw = document.documentElement.clientWidth;
          const offenders = [];
          for (const el of document.querySelectorAll('body *')) {
            const r = el.getBoundingClientRect();
            if (r.width > 0 && (r.right > vw + 1 || r.width > vw + 1)) {
              offenders.push({tag: el.tagName.toLowerCase(),
                              cls: (el.className || '').toString().slice(0, 60),
                              right: Math.round(r.right), width: Math.round(r.width)});
              if (offenders.length >= 10) break;
            }
          }
          return {inner_width: vw, scroll_width: document.documentElement.scrollWidth,
                  horizontal_overflow: document.documentElement.scrollWidth > vw + 1,
                  offenders};
        }"""

        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=True)
            except Exception as exc:  # no installed browser is a skip, not a failed assertion
                pytest.skip(f"chromium is unavailable: {type(exc).__name__}")
            page = browser.new_page(viewport={"width": 390, "height": 664})
            page.goto(f"http://127.0.0.1:{port}/login")
            page.fill("#token", TOKEN)
            page.click("button[type=submit]")
            page.wait_for_url(f"http://127.0.0.1:{port}/")
            pages = ["/", f"/project/{seeded.project_id}", f"/execution/{RUN}",
                     f"/diff/{seeded.project_id}"]
            reports = []
            for path in pages:
                page.goto(f"http://127.0.0.1:{port}{path}")
                reports.append(page.evaluate(overflow_script))
            browser.close()
        assert not [report for report in reports if report["horizontal_overflow"]], reports
        assert all(report["inner_width"] == 390 for report in reports), reports
    finally:
        server.should_exit = True
        thread.join(timeout=5)


# ---------------------------------------------------------------- helpers stay internal


def test_artifact_resolution_refuses_paths_outside_evidence_and_reports(kh_root):
    (kh_root / "evidence" / "ok").mkdir(parents=True)
    (kh_root / "evidence" / "ok" / "file.txt").write_text("ok", encoding="utf-8")
    assert data.resolve_artifact("evidence/ok/file.txt") is not None
    assert data.resolve_artifact("data/profiles/dashboard/token") is None
    assert data.resolve_artifact("/etc/passwd") is None
    assert data.resolve_artifact("../outside.txt") is None
    assert data.resolve_artifact("evidence/ok/missing.txt") is None
