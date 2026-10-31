"""API Call Tool acceptance tests (Phase 1 sections 5 and 12, "API").

Against the local mock server only - never a real production API. Covers verbs, bodies,
auth, timing, retries, concurrency, GraphQL, OpenAPI discovery and every safety gate:
target allowlist, redirect re-validation, destructive-method approval, size caps,
upload confinement and secret redaction.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from core.shared import http_client as hc
from core.shared import paths
from core.shared.config import Config, current
from core.shared.http_client import PolicyViolation, RequestSpec, TargetDenied

pytestmark = pytest.mark.api


def req(method: str, url: str, **kwargs):
    return hc.execute(RequestSpec(method=method, url=url, **kwargs))


# ---------------------------------------------------------------- verbs & payloads


def test_get_succeeds_and_measures_duration(mock_server):
    result = req("GET", f"{mock_server}/health")
    assert result["ok"] is True
    assert result["status_code"] == 200
    assert result["body"]["service"] == "khunkhaar-mock"
    assert result["duration_ms"] > 0
    assert result["redirect_hops"] == 0
    assert result["evidence"]["request"].startswith("evidence/")


def test_post_json_succeeds(mock_server):
    result = req("POST", f"{mock_server}/items", json_body={"name": "tracer", "qty": 3})
    assert result["ok"] and result["status_code"] == 200
    assert result["body"]["created"] is True
    assert result["body"]["echo"]["name"] == "tracer"


@pytest.mark.parametrize("method", ["PUT", "PATCH"])
def test_update_verbs(mock_server, method):
    result = req(method, f"{mock_server}/items/7", json_body={"name": "updated"})
    assert result["ok"], result
    assert result["body"]["echo"]["name"] == "updated"
    assert result["body"][("replaced" if method == "PUT" else "patched")] == 7


def test_query_parameters_are_applied(mock_server):
    result = req("GET", f"{mock_server}/items", query={"limit": 2, "q": "widget"})
    assert result["ok"]
    assert result["body"]["count"] == 2
    assert result["body"]["query"] == "widget"
    assert len(result["body"]["items"]) == 2
    assert "widget" in result["url"]


def test_form_encoded_post(mock_server):
    result = req("POST", f"{mock_server}/form", form_data={"credential": "s3cret-value", "name": "qa"})
    assert result["ok"], result
    assert result["body"]["credential_present"] is True
    assert "s3cret-value" not in json.dumps(result), "a form secret leaked into the tool output"
    evidence = Path(paths.kh_root() / result["evidence"]["request"]).read_text()
    assert "s3cret-value" not in evidence, "a form secret leaked into evidence"


def test_multipart_upload_of_a_file_inside_the_root(mock_server):
    fixture = paths.tmp_dir() / "api-upload-fixture.txt"
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture.write_text("khunkhaar upload fixture\n", encoding="utf-8")
    result = req("POST", f"{mock_server}/upload",
                 multipart_files=[{"path": "runtime/tmp/api-upload-fixture.txt",
                                   "field": "file", "content_type": "text/plain"}],
                 form_data={"note": "acceptance"})
    assert result["ok"], result
    assert result["body"]["received_bytes"] == len("khunkhaar upload fixture\n")
    assert result["body"]["note"] == "acceptance"
    assert result["body"]["filename"] == "api-upload-fixture.txt"


@pytest.mark.parametrize("path", ["../../etc/passwd", "~/.ssh/id_rsa", "/etc/shadow"])
def test_upload_paths_outside_the_root_are_refused(mock_server, path):
    with pytest.raises(PolicyViolation):
        req("POST", f"{mock_server}/upload", multipart_files=[{"path": path}])


def test_oversized_request_body_is_refused(mock_server):
    cfg = _cfg_with(api_max_request_bytes=1024)
    with pytest.raises(PolicyViolation):
        hc.execute(RequestSpec(method="POST", url=f"{mock_server}/items",
                               json_body={"blob": "x" * 4096}), cfg=cfg)


# ---------------------------------------------------------------- errors & timing


def test_invalid_request_is_handled_as_a_result_not_an_exception(mock_server):
    result = req("GET", f"{mock_server}/bad-request")
    assert result["ok"] is False
    assert result["status_code"] == 400
    assert result["body"]["error"] == "invalid_payload"
    assert result["error"] is None or result["error"] == ""


def test_server_error_is_reported_with_evidence(mock_server):
    result = req("GET", f"{mock_server}/server-error")
    assert result["ok"] is False and result["status_code"] == 500
    assert Path(paths.kh_root() / result["evidence"]["response"]).is_file()


def test_timeout_is_handled(mock_server):
    result = req("GET", f"{mock_server}/slow", query={"delay": 2}, timeout_seconds=0.4)
    assert result["ok"] is False
    assert result["timed_out"] is True
    assert result["error_kind"] == "timeout"
    assert result["status_code"] is None
    assert result["duration_ms"] < 3000


def test_timeout_is_clamped_to_the_configured_ceiling(mock_server):
    assert current().api_timeout(9999) == float(current().get("api", "max_timeout_seconds"))
    assert current().api_timeout(None) == float(current().get("api", "default_timeout_seconds"))


def test_bounded_retries_recover_a_flaky_endpoint(mock_server):
    result = req("GET", f"{mock_server}/flaky", retries=2)
    assert result["ok"], result
    assert result["attempts"] == 3
    assert result["retried_after_status"] == 503


def test_retry_count_is_clamped(mock_server):
    assert current().api_max_retries(1000) == int(current().get("api", "max_retries"))
    assert current().api_max_retries(None) == 0


def test_response_size_cap_marks_truncation(mock_server):
    cfg = _cfg_with(api_max_response_bytes=4096)
    result = hc.execute(RequestSpec(method="GET", url=f"{mock_server}/large",
                                    query={"kb": 64}, save_evidence=False), cfg=cfg)
    assert result["body_kind"] == "truncated"
    assert result["body_bytes"] == 64 * 1024


def test_concurrent_execution(mock_server):
    specs = [RequestSpec(method="GET", url=f"{mock_server}/health", run_id="api-concurrency")
             for _ in range(6)]
    import asyncio

    results = asyncio.run(hc.execute_many(specs, concurrency=3))
    assert len(results) == 6
    assert all(r["ok"] for r in results), results


# ---------------------------------------------------------------- auth & redaction


def test_sensitive_values_are_redacted_in_headers_and_body(mock_server):
    result = req("GET", f"{mock_server}/secret-response")
    assert result["ok"]
    assert result["body"]["api_token"] != "tok_live_abcdef123456"
    assert "REDACTED" in str(result["body"]["api_token"])
    assert "supersecret-value" not in json.dumps(result["response_headers"])
    assert "REDACTED" in str(result["response_headers"]["set-cookie"])
    # non-secret headers must survive, otherwise the tool is useless for assertions
    assert result["response_headers"]["x-ratelimit-remaining"] == "42"


def test_evidence_files_are_redacted_too(mock_server):
    result = req("GET", f"{mock_server}/secret-response", run_id="api-redaction-check")
    on_disk = Path(paths.kh_root() / result["evidence"]["response"]).read_text()
    assert "tok_live_abcdef123456" not in on_disk
    assert "supersecret-value" not in on_disk
    request_on_disk = Path(paths.kh_root() / result["evidence"]["request"]).read_text()
    assert "tok_live_abcdef123456" not in request_on_disk


def test_auth_profile_from_an_environment_variable_is_attached_but_never_echoed(monkeypatch, mock_server):
    cfg = _cfg_with(auth_profiles=[{"name": "phase1-test", "env_var": "KHUNKHAAR_TEST_TOKEN",
                                    "header": "X-Api-Key"}])
    monkeypatch.setenv("KHUNKHAAR_TEST_TOKEN", "env-secret-do-not-log")
    import core.shared.config as config_module

    monkeypatch.setattr(config_module, "_CURRENT", cfg)
    result = req("GET", f"{mock_server}/auth/whoami", auth_profile="phase1-test")
    assert result["ok"] and result["body"]["has_x_api_key"] is True
    assert "env-secret-do-not-log" not in json.dumps(result)
    evidence = Path(paths.kh_root() / result["evidence"]["request"]).read_text()
    assert "env-secret-do-not-log" not in evidence, "credential written to evidence in plaintext"


def test_unknown_auth_profile_is_a_structured_error(api_mcp, mock_server):
    result = api_mcp.api_request(method="GET", url=f"{mock_server}/health",
                                 auth_profile="does-not-exist")
    assert result["ok"] is False
    assert result["error_kind"] == "unknown_auth_profile"


def test_raw_authorization_header_supplied_by_the_model_is_still_redacted(mock_server):
    result = req("GET", f"{mock_server}/auth/whoami",
                 headers={"Authorization": "Bearer pasted-token-123"})
    assert result["ok"] and result["body"]["has_authorization"] is True
    assert result["body"]["auth_scheme"] == "Bearer"
    assert "pasted-token-123" not in json.dumps(result)
    request_evidence = Path(paths.kh_root() / result["evidence"]["request"]).read_text()
    assert "pasted-token-123" not in request_evidence


# ---------------------------------------------------------------- target guard


def test_hosts_outside_the_allowlist_are_refused(api_mcp):
    for url in ("https://example.com/", "http://93.184.216.34/", "https://api.github.com/"):
        result = api_mcp.api_request(method="GET", url=url)
        assert result["ok"] is False and result["error_kind"] == "target_denied", (url, result)


def test_cloud_metadata_targets_are_refused(api_mcp):
    for url in ("http://169.254.169.254/latest/meta-data/",
                "http://metadata.google.internal/computeMetadata/v1/"):
        result = api_mcp.api_request(method="GET", url=url)
        assert result["ok"] is False and result["error_kind"] == "target_denied", (url, result)


def test_redirect_targets_are_re_validated(mock_server, api_mcp):
    """The engine raises; the MCP layer is the boundary that turns it into a structured result."""
    for target in ("http://example.com/evil", "http://169.254.169.254/", "http://127.0.0.1:9999/"):
        with pytest.raises(TargetDenied):
            req("GET", f"{mock_server}/redirect-via", query={"to": target})
        result = api_mcp.api_request(method="GET", url=f"{mock_server}/redirect-via",
                                     query={"to": target})
        assert result["ok"] is False and result["error_kind"] == "target_denied", (target, result)
        assert "hop 1" in result["error"], result["error"]


def test_allowlisted_redirect_is_followed(mock_server):
    result = req("GET", f"{mock_server}/redirect-via", query={"to": f"{mock_server}/health"})
    assert result["ok"] and result["status_code"] == 200
    assert result["redirect_hops"] == 1
    assert result["body"]["status"] == "ok"


def test_api_check_target_previews_without_requesting(api_mcp, mock_server):
    assert api_mcp.api_check_target(url=f"{mock_server}/health")["allowed"] is True
    denied = api_mcp.api_check_target(url="https://example.com/")
    assert denied["allowed"] is False and denied["reason"]


# ---------------------------------------------------------------- destructive gate


def test_delete_is_blocked_by_default(api_mcp, mock_server):
    result = api_mcp.api_request(method="DELETE", url=f"{mock_server}/items/1")
    assert result["ok"] is False and result["error_kind"] == "policy_violation"
    assert "allow_destructive" in result["error"]


def test_delete_still_requires_operator_approval(api_mcp, mock_server):
    """allow_destructive is the caller's flag; operator approval lives in security.*."""
    result = api_mcp.api_request(method="DELETE", url=f"{mock_server}/items/1",
                                 allow_destructive=True)
    assert result["ok"] is False and result["error_kind"] == "policy_violation"
    assert "destructive_approved_targets" in result["error"]


def test_delete_executes_once_the_operator_approves_that_host(mock_server):
    cfg = _cfg_with(destructive_approved=["127.0.0.1"])
    result = hc.execute(RequestSpec(method="DELETE", url=f"{mock_server}/items/3",
                                    allow_destructive=True), cfg=cfg)
    assert result["ok"], result
    assert result["body"]["deleted"] == 3


def test_delete_approval_does_not_extend_to_other_hosts(mock_server):
    cfg = _cfg_with(destructive_approved=["127.0.0.1:9999"])
    with pytest.raises(PolicyViolation):
        hc.execute(RequestSpec(method="DELETE", url=f"{mock_server}/items/3",
                               allow_destructive=True), cfg=cfg)


@pytest.mark.parametrize("method", ["TRACE", "CONNECT", "BOGUS"])
def test_unsupported_methods_are_refused(mock_server, method):
    with pytest.raises(PolicyViolation):
        hc.execute(RequestSpec(method=method, url=f"{mock_server}/health"))


# ---------------------------------------------------------------- graphql & openapi


def test_graphql_query_and_variables(api_mcp, mock_server):
    result = api_mcp.api_graphql(url=f"{mock_server}/graphql",
                                 query="query Users($limit: Int) { users { id name } }",
                                 variables={"limit": 2})
    assert result["ok"], result
    assert result["body"]["data"]["users"][0]["name"] == "ada"


def test_graphql_errors_are_surfaced_separately(api_mcp, mock_server):
    result = api_mcp.api_graphql(url=f"{mock_server}/graphql", query="{ fail }")
    assert result["ok"] is False
    assert result["graphql_errors"], result
    assert "doesn't exist" in str(result["graphql_errors"])
    # transport said 200 - the tool must not report this as a passing call
    assert result["status_code"] == 200


def test_openapi_discovery_lists_operations(api_mcp, mock_server):
    doc = api_mcp.api_discover_openapi(url=f"{mock_server}/openapi.json")
    assert doc["ok"], doc
    assert doc["operation_count"] >= 15
    verbs = {op["method"] for op in doc["operations"]}
    assert {"GET", "POST", "DELETE"} <= verbs
    assert any(op["path"] == "/items" for op in doc["operations"])


def test_openapi_discovery_rejects_a_non_schema_document(api_mcp, mock_server):
    result = api_mcp.api_discover_openapi(url=f"{mock_server}/no-schema")
    assert result["ok"] is False
    assert "OpenAPI" in result["error"] or "JSON" in result["error"]


# ---------------------------------------------------------------- policy surface


def test_api_policy_exposes_profile_names_but_never_values(api_mcp):
    policy = api_mcp.api_policy()
    assert policy["ok"]
    assert "127.0.0.1:8731" in policy["allowed_hosts"]
    assert "169.254.169.254" in policy["denied_hosts"]
    assert policy["destructive_methods"] == ["DELETE"]
    assert isinstance(policy["auth_profiles"], list)
    for entry in policy["auth_profiles"]:
        assert "=" not in str(entry), "a credential assignment leaked into the policy listing"


def test_batch_tool_validates_each_entry(api_mcp, mock_server):
    import asyncio

    batch = asyncio.run(api_mcp.api_request_many(requests=[
        {"method": "GET", "url": f"{mock_server}/health"},
        {"method": "GET", "url": "https://example.com/"},
    ], concurrency=2))
    assert batch["requested"] == 2
    assert batch["succeeded"] == 1
    assert batch["results"][1]["error_kind"] == "target_denied"

    invalid = asyncio.run(api_mcp.api_request_many(requests=[{"url": "http://x"}]))
    assert invalid["ok"] is False and invalid["error_kind"] == "invalid_input"


# ---------------------------------------------------------------- helpers


def _cfg_with(**overrides) -> Config:
    raw = copy.deepcopy(current().raw)
    if "api_max_request_bytes" in overrides:
        raw["api"]["max_request_bytes"] = overrides["api_max_request_bytes"]
    if "api_max_response_bytes" in overrides:
        raw["api"]["max_response_bytes"] = overrides["api_max_response_bytes"]
    if "destructive_approved" in overrides:
        raw["security"]["destructive_approved_targets"] = overrides["destructive_approved"]
    if "auth_profiles" in overrides:
        raw["auth"] = {"profiles": overrides["auth_profiles"]}
    return Config(raw=raw)
