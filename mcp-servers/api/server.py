"""KHUNKHAAR API Call Tool — MCP server.

Exposes the hardened HTTP engine (core/shared/http_client.py) to MCP clients such as OpenCode.
The engine enforces safety; this layer only translates MCP input into RequestSpec objects and
returns structured, redacted results.

Run: ~/KHUNKHAAR-QA/.venv/bin/python ~/KHUNKHAAR-QA/mcp-servers/api/server.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp.server.mcpserver import MCPServer  # noqa: E402

from core.shared import evidence as ev  # noqa: E402
from core.shared import http_client as hc  # noqa: E402
from core.shared.config import current  # noqa: E402
from core.shared.credentials import list_profiles  # noqa: E402
from core.shared.http_client import PolicyViolation, RequestSpec, TargetDenied  # noqa: E402
from core.shared.netguard import check_target  # noqa: E402
from core.shared.toollog import get_logger  # noqa: E402

logger = get_logger("mcp_api", "mcp_api.log")

server = MCPServer(
    name="khunkhaar-api",
    version="0.1.0",
    instructions=(
        "HTTP API testing for KHUNKHAAR QA. Targets must appear in api.allowed_hosts in "
        "config/khunkhaar.yaml. DELETE needs allow_destructive=true AND an operator-approved host in "
        "security.destructive_approved_targets. Authorization headers and secret-looking fields are "
        "redacted from every result; prefer auth_profile over pasting a token."
    ),
)


def _denial(exc: Exception, kind: str) -> dict[str, Any]:
    return {
        "ok": False,
        "status_code": None,
        "error_kind": kind,
        "error": str(exc),
        "body": None,
        "evidence": None,
    }


@server.tool(description="Send one HTTP request (GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS) and return status, redacted headers, redacted body and duration. Refuses targets outside api.allowed_hosts and destructive methods without operator approval.")
def api_request(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    query: dict[str, Any] | None = None,
    json_body: Any = None,
    form_data: dict[str, Any] | None = None,
    multipart_files: list[dict[str, str]] | None = None,
    auth_profile: str | None = None,
    timeout_seconds: float | None = None,
    retries: int = 0,
    allow_destructive: bool = False,
    save_evidence: bool = True,
    run_id: str | None = None,
) -> dict[str, Any]:
    spec = RequestSpec(
        method=method, url=url, headers=headers or {}, query=query or {}, json_body=json_body,
        form_data=form_data, multipart_files=multipart_files, auth_profile=auth_profile,
        timeout_seconds=timeout_seconds, retries=retries, allow_destructive=allow_destructive,
        save_evidence=save_evidence, run_id=run_id,
    )
    try:
        return hc.execute(spec)
    except TargetDenied as exc:
        return _denial(exc, "target_denied")
    except PolicyViolation as exc:
        return _denial(exc, "policy_violation")
    except KeyError as exc:
        return _denial(exc, "unknown_auth_profile")


@server.tool(description="Execute several requests with bounded concurrency (max 20) against allowlisted targets. Each result is independently validated and redacted.")
async def api_request_many(
    requests: list[dict[str, Any]],
    concurrency: int = 5,
    run_id: str | None = None,
) -> dict[str, Any]:
    specs = []
    for item in requests:
        if not isinstance(item, dict) or "method" not in item or "url" not in item:
            return {"ok": False, "error_kind": "invalid_input",
                    "error": "each entry needs at least 'method' and 'url'", "results": []}
        specs.append(RequestSpec(
            method=item["method"], url=item["url"], headers=item.get("headers") or {},
            query=item.get("query") or {}, json_body=item.get("json_body"),
            auth_profile=item.get("auth_profile"), timeout_seconds=item.get("timeout_seconds"),
            retries=item.get("retries", 0), allow_destructive=item.get("allow_destructive", False),
            run_id=run_id or ev.new_run_id("api-batch"),
        ))
    try:
        results = await hc.execute_many(specs, concurrency=concurrency)
    except (TargetDenied, PolicyViolation) as exc:
        return {"ok": False, "error": str(exc), "results": []}
    return {
        "ok": all(r.get("ok") for r in results),
        "requested": len(results),
        "succeeded": sum(1 for r in results if r.get("ok")),
        "concurrency": concurrency,
        "results": results,
    }


@server.tool(description="Run a GraphQL query or mutation against an allowlisted endpoint. GraphQL-level 'errors' are surfaced separately from transport status.")
def api_graphql(
    url: str,
    query: str,
    variables: dict[str, Any] | None = None,
    operation_name: str | None = None,
    headers: dict[str, str] | None = None,
    auth_profile: str | None = None,
    timeout_seconds: float | None = None,
    save_evidence: bool = True,
) -> dict[str, Any]:
    try:
        return hc.graphql(url, query, variables, operation_name,
                          headers=headers or {}, auth_profile=auth_profile,
                          timeout_seconds=timeout_seconds, save_evidence=save_evidence)
    except TargetDenied as exc:
        return _denial(exc, "target_denied")
    except PolicyViolation as exc:
        return _denial(exc, "policy_violation")
    except KeyError as exc:
        return _denial(exc, "unknown_auth_profile")


@server.tool(description="Fetch an OpenAPI/Swagger document (e.g. /openapi.json) and return the operation list, servers and spec version for test planning.")
def api_discover_openapi(url: str, timeout_seconds: float | None = None) -> dict[str, Any]:
    try:
        return hc.discover_openapi(url, timeout=timeout_seconds)
    except TargetDenied as exc:
        return _denial(exc, "target_denied")


@server.tool(description="Inspect the effective API safety policy: allowed and denied targets, permitted methods, timeout and retry ceilings, and configured auth profile names (names only, never values).")
def api_policy() -> dict[str, Any]:
    cfg = current()
    api = cfg.section("api")
    return {
        "ok": True,
        "allowed_hosts": api.get("allowed_hosts", []),
        "denied_hosts": api.get("denied_hosts", []),
        "allow_private_ranges": api.get("allow_private_ranges"),
        "safe_methods": api.get("safe_methods"),
        "mutating_methods": api.get("mutating_methods"),
        "destructive_methods": api.get("destructive_methods"),
        "destructive_approved_targets": cfg.get("security", "destructive_approved_targets", []),
        "require_approval_for_mutating": api.get("require_approval_for_mutating"),
        "default_timeout_seconds": api.get("default_timeout_seconds"),
        "max_timeout_seconds": api.get("max_timeout_seconds"),
        "max_retries": api.get("max_retries"),
        "max_response_bytes": api.get("max_response_bytes"),
        "auth_profiles": list_profiles(),
        "note": "no target allowlist means every request is refused",
    }


@server.tool(description="Check whether a single URL would be permitted right now, including DNS and redirect-target resolution, without making the request.")
def api_check_target(url: str) -> dict[str, Any]:
    decision = check_target(url, current())
    return {"ok": decision.allowed, "allowed": decision.allowed, "reason": decision.reason,
            "host": decision.host, "port": decision.port, "scheme": decision.scheme}


if __name__ == "__main__":
    logger.info("starting khunkhaar-api MCP server over stdio")
    server.run(transport="stdio")
