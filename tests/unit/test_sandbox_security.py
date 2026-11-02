"""Security and isolation tests for KHUNKHAAR-QA Ephemeral Sandbox."""
from __future__ import annotations

import os
from pathlib import Path
import pytest

from core.sandbox.filesystem import PathTraversalAttempt, validate_safe_path
from core.sandbox.models import NetworkPolicy, SandboxJob, JobStatus
from core.sandbox.policy import PolicyViolation, validate_sandbox_job, validate_target_url
from core.sandbox.runner import execute_in_sandbox, terminate_and_cleanup
from core.sandbox.tools import get_execution_artifacts, get_execution_logs, run_in_sandbox


def test_path_traversal_detection(tmp_path: Path):
    safe_base = tmp_path / "sandbox"
    safe_base.mkdir()
    escape_attempt = safe_base / ".." / "etc" / "passwd"
    with pytest.raises(PathTraversalAttempt):
        validate_safe_path(escape_attempt, safe_base)


def test_policy_denies_cloud_metadata():
    with pytest.raises(PolicyViolation, match="cloud metadata"):
        validate_target_url("http://169.254.169.254/latest/meta-data/", NetworkPolicy.ALLOWLISTED)

    with pytest.raises(PolicyViolation, match="cloud metadata"):
        validate_target_url("http://100.100.100.200/latest/meta-data/", NetworkPolicy.ALLOWLISTED)


def test_policy_denies_network_under_none_policy():
    with pytest.raises(PolicyViolation, match="strictly DENIED"):
        validate_target_url("http://127.0.0.1:8731/api", NetworkPolicy.NONE)


def test_policy_enforces_mock_only():
    # Valid mock server URL
    valid = validate_target_url("http://127.0.0.1:8731/api/users", NetworkPolicy.MOCK_ONLY)
    assert valid == "http://127.0.0.1:8731/api/users"

    # Non-mock port should be rejected
    with pytest.raises(PolicyViolation, match="mock_only"):
        validate_target_url("http://127.0.0.1:8080/api", NetworkPolicy.MOCK_ONLY)

    # External domain should be rejected
    with pytest.raises(PolicyViolation, match="mock_only"):
        validate_target_url("https://google.com:443", NetworkPolicy.MOCK_ONLY)


def test_sandbox_executes_python_safely():
    script = "print('HELLO_FROM_SANDBOX_ISOLATION')"
    res = run_in_sandbox(script=script, language="python")
    assert res["status"] == "COMPLETED"
    assert res["exit_code"] == 0
    assert "HELLO_FROM_SANDBOX_ISOLATION" in res["stdout"]


def test_sandbox_terminates_timeout():
    script = "import time\ntime.sleep(10)\nprint('DONE')"
    res = run_in_sandbox(script=script, language="python", timeout_seconds=1)
    assert res["status"] == "TIMED_OUT"
    assert "TIMEOUT" in res["stderr"]


def test_sandbox_captures_errors_and_logs():
    script = "import sys\nsys.stderr.write('CRITICAL_SANDBOX_FAULT')\nsys.exit(42)"
    res = run_in_sandbox(script=script, language="python")
    assert res["status"] == "FAILED"
    assert res["exit_code"] == 42
    assert "CRITICAL_SANDBOX_FAULT" in res["stderr"]

    logs = get_execution_logs(res["execution_id"])
    assert "CRITICAL_SANDBOX_FAULT" in logs["stderr"]


def test_sandbox_quarantines_artifacts():
    script = (
        "import os\n"
        "art_dir = os.environ.get('KHUNKHAAR_ARTIFACTS_DIR', '.')\n"
        "with open(f'{art_dir}/report.txt', 'w') as f:\n"
        "    f.write('SANDBOX_REPORT_DATA')\n"
    )
    res = run_in_sandbox(script=script, language="python", collect_artifacts=True)
    assert res["status"] == "COMPLETED"
    assert len(res["artifacts"]) >= 1
    assert res["artifacts"][0]["name"] == "report.txt"
    assert res["artifacts"][0]["checksum"] != ""

    arts = get_execution_artifacts(res["execution_id"])
    assert len(arts) >= 1
    assert arts[0]["name"] == "report.txt"

    # Cleanup
    terminate_and_cleanup(res["execution_id"])


def test_sandbox_executes_node():
    res = run_in_sandbox("console.log('NODE_UNIT_TEST');", language="javascript", execution_profile="node_qa")
    assert res["status"] == "COMPLETED"
    assert "NODE_UNIT_TEST" in res["stdout"]


def test_sandbox_executes_playwright_profile():
    res = run_in_sandbox("import playwright\nprint('PLAYWRIGHT_UNIT_TEST')", language="python", execution_profile="playwright_qa")
    assert res["status"] == "COMPLETED"
    assert "PLAYWRIGHT_UNIT_TEST" in res["stdout"]


def test_sandbox_full_validation_checklist():
    from core.sandbox.validator import run_all_validation_checks
    items = run_all_validation_checks()
    assert len(items) == 18
    assert all(i.status == "PASS" for i in items)

