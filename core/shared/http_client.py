"""HTTP engine behind the API Call Tool.

Used by the MCP server and by the infrastructure tests, so both exercise the same code path.
Safety is enforced here rather than at the MCP layer: method policy, target allowlist, per-hop
redirect re-validation, timeouts, bounded retries, size caps, redaction and evidence capture.
"""

from __future__ import annotations

import asyncio
import json as jsonlib
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx

from . import evidence as ev
from . import netguard, paths
from .config import Config, current
from .credentials import resolve as resolve_auth
from .redaction import redact_headers, redact_obj, redact_text

MAX_REDIRECTS = 5


class PolicyViolation(RuntimeError):
    pass


class TargetDenied(RuntimeError):
    pass


@dataclass
class RequestSpec:
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    query: dict[str, Any] = field(default_factory=dict)
    json_body: Any = None
    form_data: dict[str, Any] | None = None
    multipart_files: list[dict[str, str]] | None = None
    auth_profile: str | None = None
    timeout_seconds: float | None = None
    retries: int = 0
    allow_destructive: bool = False
    save_evidence: bool = True
    run_id: str | None = None


def _final_url(url: str, query: dict[str, Any]) -> str:
    if not query:
        return url
    parts = urlsplit(url)
    existing = dict(pair.split("=", 1) for pair in parts.query.split("&") if "=" in pair)
    merged = {**existing, **{k: ("" if v is None else v) for k, v in query.items()}}
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(merged, doseq=True), parts.fragment))


def check_method_policy(spec: RequestSpec, cfg: Config) -> None:
    api = cfg.section("api")
    method = spec.method.upper()
    safe = [m.upper() for m in api.get("safe_methods", [])]
    mutating = [m.upper() for m in api.get("mutating_methods", [])]
    destructive = [m.upper() for m in api.get("destructive_methods", [])]

    if method not in safe + mutating + destructive:
        raise PolicyViolation(f"HTTP method '{method}' is not supported by this tool")

    if method in destructive:
        if not spec.allow_destructive:
            raise PolicyViolation(
                f"'{method}' is a destructive method and is blocked by default. "
                "Re-send with allow_destructive=true after explicit human approval."
            )
        host = netguard.normalize_host(urlsplit(spec.url).hostname or "")
        port = urlsplit(spec.url).port
        approved = [str(h).lower() for h in cfg.get("security", "destructive_approved_targets", []) or []]
        if api.get("require_approval_for_destructive", True) and not any(
            netguard.host_matches(entry, host, port) for entry in approved
        ):
            raise PolicyViolation(
                f"'{method}' additionally requires operator approval: '{host}'"
                f"{':' + str(port) if port else ''} is not in security.destructive_approved_targets "
                f"(currently {approved or 'empty'}). The caller's allow_destructive flag is not approval."
            )

    if method in mutating and api.get("require_approval_for_mutating", False):
        raise PolicyViolation(
            f"'{method}' requires operator approval (api.require_approval_for_mutating is enabled)"
        )


def _multipart_paths_ok(files: list[dict[str, str]], cfg: Config) -> None:
    """Only allow uploading fixtures that live inside the KHUNKHAAR tree."""
    root = paths.kh_root().resolve()
    for item in files:
        raw = item.get("path") or ""
        candidate = paths.Path(raw).expanduser()
        candidate = candidate if candidate.is_absolute() else (root / raw)
        try:
            resolved = candidate.resolve()
        except OSError:
            raise PolicyViolation(f"cannot resolve upload path: {raw}")
        if root not in resolved.parents and resolved != root:
            raise PolicyViolation(
                f"upload path '{raw}' is outside the KHUNKHAAR root and will not be sent"
            )
        if not resolved.is_file():
            raise PolicyViolation(f"upload file not found: {raw}")


def _build_request(spec: RequestSpec, cfg: Config) -> dict[str, Any]:
    url = _final_url(spec.url, spec.query or {})
    decision = netguard.check_target(url, cfg)
    if not decision.allowed:
        raise TargetDenied(decision.reason)

    headers = {str(k): str(v) for k, v in (spec.headers or {}).items()}
    if spec.auth_profile:
        for key, value in resolve_auth(spec.auth_profile).headers.items():
            headers[key] = value

    kwargs: dict[str, Any] = {"headers": headers}

    has_body = spec.json_body is not None or spec.form_data is not None or spec.multipart_files
    if has_body:
        payload_size = len(jsonlib.dumps(spec.json_body).encode()) if spec.json_body is not None else 0
        limit = int(cfg.get("api", "max_request_bytes", 2_000_000))
        if payload_size > limit:
            raise PolicyViolation(f"request body is {payload_size} bytes, limit is {limit}")

    if spec.json_body is not None:
        kwargs["json"] = spec.json_body
    elif spec.multipart_files:
        _multipart_paths_ok(spec.multipart_files, cfg)
        root = paths.kh_root().resolve()
        opened = []
        files_arg = []
        for item in spec.multipart_files:
            resolved = (root / item["path"]).resolve() if not str(item["path"]).startswith("/") else paths.Path(item["path"]).resolve()
            fh = open(resolved, "rb")
            opened.append(fh)
            files_arg.append((item.get("field", "file"), (item.get("filename", resolved.name), fh, item.get("content_type", "application/octet-stream"))))
        kwargs["files"] = files_arg
        if spec.form_data:
            kwargs["data"] = spec.form_data
        kwargs["_open_files"] = opened
    elif spec.form_data is not None:
        kwargs["data"] = spec.form_data

    return {"url": url, "kwargs": kwargs, "decision": decision}


def _decode_body(response: httpx.Response, cfg: Config) -> tuple[Any, str]:
    ctype = response.headers.get("content-type", "")
    limit = int(cfg.get("api", "max_response_bytes", 5_000_000))
    raw = response.content[:limit]
    truncated = len(response.content) > limit
    text = raw.decode("utf-8", errors="replace")
    if "json" in ctype.lower():
        try:
            return jsonlib.loads(text), ("truncated" if truncated else "json")
        except jsonlib.JSONDecodeError:
            pass
    return (text if truncated or not text else text), ("truncated" if truncated else "text")


def _do_single(spec: RequestSpec, cfg: Config) -> dict[str, Any]:
    built = _build_request(spec, cfg)
    timeout = cfg.api_timeout(spec.timeout_seconds)
    kwargs = dict(built["kwargs"])
    open_files = kwargs.pop("_open_files", None)

    started = time.monotonic()
    hops: list[str] = []
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            url = built["url"]
            for hop in range(MAX_REDIRECTS + 1):
                decision = netguard.check_target(url, cfg)
                if not decision.allowed:
                    raise TargetDenied(f"redirect target refused (hop {hop}): {decision.reason}")
                hops.append(url)
                response = client.request(spec.method.upper(), url, **kwargs)
                if response.status_code in (301, 302, 303, 307, 308) and "location" in response.headers:
                    url = httpx.URL(url).join(response.headers["location"]).__str__()
                    if hop == MAX_REDIRECTS:
                        raise TargetDenied("too many redirects")
                    if response.status_code in (301, 302, 303) and spec.method.upper() != "GET":
                        kwargs = {"headers": kwargs.get("headers", {})}  # match browser semantics
                        continue
                    continue
                break
    finally:
        for fh in open_files or []:
            try:
                fh.close()
            except OSError:
                pass

    elapsed_ms = round((time.monotonic() - started) * 1000, 2)
    body, body_kind = _decode_body(response, cfg)
    body_char_limit = int(cfg.get("api", "max_body_chars_in_output", 20_000))

    result: dict[str, Any] = {
        "ok": 200 <= response.status_code < 400,
        "method": spec.method.upper(),
        "url": str(response.url),
        "status_code": response.status_code,
        "reason_phrase": response.reason_phrase,
        "duration_ms": elapsed_ms,
        "response_headers": redact_headers(response.headers),
        "redirect_hops": len(hops) - 1,
        "body_kind": body_kind,
        "body": redact_obj(
            body if isinstance(body, (dict, list)) else redact_text(str(body))[:body_char_limit]
        ),
        "body_bytes": len(response.content),
        "timed_out": False,
        "error": None,
    }
    result["evidence"] = _persist(spec, cfg, result, response) if spec.save_evidence else None
    return result


def _persist(spec: RequestSpec, cfg: Config, result: dict, response: httpx.Response) -> dict[str, str]:
    run_id = spec.run_id or ev.new_run_id("api")
    folder = ev.evidence_root("api", run_id)
    request_summary = {
        "method": spec.method.upper(),
        "url": result["url"],
        "headers": redact_headers(spec.headers or {}),
        "auth_profile": spec.auth_profile or None,
        "query": redact_obj(spec.query or {}),
        "json_body": redact_obj(spec.json_body),
        "form_data": redact_obj(spec.form_data) if spec.form_data else None,
        "multipart_files": redact_obj(spec.multipart_files) if spec.multipart_files else None,
    }
    written = {
        "request": ev.write_json(folder, "request.json", request_summary),
        "response": ev.write_json(folder, "response.json", {
            "status_code": result["status_code"],
            "duration_ms": result["duration_ms"],
            "response_headers": result["response_headers"],
            "body": result["body"],
        }),
    }
    ctype = response.headers.get("content-type", "")
    if "octet-stream" in ctype.lower() or "image" in ctype.lower():
        written["payload"] = ev.write_bytes(folder, "response.payload", response.content)
    return {k: ev.relative(v) for k, v in written.items()}


def _failure(spec: RequestSpec, cfg: Config, exc: Exception, elapsed_ms: float, kind: str) -> dict[str, Any]:
    message = redact_text(f"{type(exc).__name__}: {exc}")
    run_id = spec.run_id or ev.new_run_id("api")
    folder = ev.evidence_root("api", run_id)
    evidence = None
    if spec.save_evidence:
        written = ev.write_json(folder, "failure.json", {
            "method": spec.method.upper(),
            "url": spec.url,
            "error_kind": kind,
            "message": message,
            "duration_ms": elapsed_ms,
        })
        evidence = {"failure": ev.relative(written)}
    return {
        "ok": False,
        "method": spec.method.upper(),
        "url": spec.url,
        "status_code": None,
        "duration_ms": elapsed_ms,
        "error_kind": kind,
        "timed_out": kind == "timeout",
        "error": message,
        "body": None,
        "evidence": evidence,
    }


def execute(spec: RequestSpec, cfg: Config | None = None) -> dict[str, Any]:
    cfg = cfg or current()
    spec.method = spec.method.upper()
    check_method_policy(spec, cfg)

    attempts = cfg.api_max_retries(spec.retries) + 1
    retry_statuses = {502, 503, 504}
    last: dict[str, Any] = {}
    for attempt in range(attempts):
        started = time.monotonic()
        try:
            result = _do_single(spec, cfg)
            result["attempts"] = attempt + 1
            if result["status_code"] in retry_statuses and attempt < attempts - 1:
                last = result
                time.sleep(min(2 ** attempt * 0.2, 2.0))
                continue
            if attempt > 0 and result["ok"]:
                result["retried_after_status"] = last.get("status_code")
            return result
        except (TargetDenied, PolicyViolation):
            raise
        except httpx.TimeoutException as exc:
            return _failure(spec, cfg, exc, round((time.monotonic() - started) * 1000, 2), "timeout")
        except httpx.HTTPError as exc:
            elapsed = round((time.monotonic() - started) * 1000, 2)
            if attempt < attempts - 1 and isinstance(exc, (httpx.ConnectError, httpx.ReadError)):
                time.sleep(min(2 ** attempt * 0.2, 2.0))
                continue
            return _failure(spec, cfg, exc, elapsed, "transport_error")
    return last


async def execute_many(specs: list[RequestSpec], cfg: Config | None = None, concurrency: int = 5) -> list[dict[str, Any]]:
    """Bounded concurrent execution - used for API load-adjacent checks, never against production."""
    cfg = cfg or current()
    for spec in specs:
        spec.method = spec.method.upper()
        check_method_policy(spec, cfg)
    sem = asyncio.Semaphore(max(1, min(int(concurrency), 20)))

    async def runner(spec: RequestSpec) -> dict[str, Any]:
        async with sem:
            try:
                return await asyncio.to_thread(execute, spec, cfg)
            except TargetDenied as exc:
                return {"ok": False, "method": spec.method, "url": spec.url, "status_code": None,
                        "error_kind": "target_denied", "error": str(exc), "body": None}
            except PolicyViolation as exc:
                return {"ok": False, "method": spec.method, "url": spec.url, "status_code": None,
                        "error_kind": "policy_violation", "error": str(exc), "body": None}

    return list(await asyncio.gather(*(runner(s) for s in specs)))


def graphql(url: str, query: str, variables: dict | None, operation_name: str | None,
            cfg: Config | None = None, **kwargs: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"query": query}
    if variables:
        body["variables"] = variables
    if operation_name:
        body["operationName"] = operation_name
    spec = RequestSpec(method="POST", url=url, json_body=body, **kwargs)
    result = execute(spec, cfg)
    if isinstance(result.get("body"), dict) and "errors" in result["body"]:
        result["graphql_errors"] = redact_obj(result["body"]["errors"])
        result["ok"] = result["ok"] and not result["graphql_errors"]
    return result


def discover_openapi(url: str, cfg: Config | None = None, timeout: float | None = None) -> dict[str, Any]:
    """Fetch an OpenAPI/Swagger document and reduce it to an operation list."""
    spec = RequestSpec(method="GET", url=url, timeout_seconds=timeout, save_evidence=False)
    result = execute(spec, cfg)
    if not result["ok"] or not isinstance(result.get("body"), (dict, list)):
        return {"ok": False, "error": f"could not retrieve OpenAPI document: {result.get('error') or 'not JSON'}",
                "status_code": result.get("status_code")}
    doc = result["body"]
    if isinstance(doc, str):
        try:
            doc = jsonlib.loads(doc)
        except jsonlib.JSONDecodeError:
            return {"ok": False, "error": "OpenAPI document is not valid JSON"}
    operations = []
    for path, methods in (doc.get("paths") or {}).items():
        if not isinstance(methods, dict):
            continue
        for method, meta in methods.items():
            if method.upper() not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
                continue
            if not isinstance(meta, dict):
                continue
            operations.append({
                "method": method.upper(),
                "path": path,
                "operation_id": meta.get("operationId"),
                "summary": meta.get("summary"),
                "params": [p.get("name") for p in meta.get("parameters", []) if isinstance(p, dict)],
                "deprecated": bool(meta.get("deprecated", False)),
            })
    info = doc.get("info") or {}
    servers = [s.get("url") for s in doc.get("servers", []) if isinstance(s, dict)] or \
              ([doc["host"] + doc.get("basePath", "")] if "host" in doc else [])
    return {
        "ok": True,
        "title": info.get("title"),
        "spec_version": doc.get("openapi") or doc.get("swagger"),
        "api_version": info.get("version"),
        "servers": servers,
        "operation_count": len(operations),
        "operations": operations,
        "document_url": url,
    }
