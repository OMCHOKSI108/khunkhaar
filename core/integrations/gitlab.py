"""Read-only GitLab merge-request adapter.

This module exists so an operator can look at one merge request without opening a browser. It is
off by default, and enabling it is a two-step decision: `gitlab.enabled` must be true *and* the
configured host must already appear in `api.allowed_hosts`. The token is read at call time from
the environment variable named by `gitlab.token_env`; the value is never returned, logged or
persisted.

There is deliberately no generic request function. `_get` is the only transport and it hardcodes
`client.get`, with redirects disabled and the host re-validated on every hop, so no code path in
this module can POST, PUT, PATCH or DELETE. Diff bodies are read only to count pages; only per-file
identities are returned, never the diff text.

Any failure - disabled configuration, a missing token, an unreachable host, a 401 - returns
`status: "unavailable"` (or `"refused"` when the host is not allowlisted) with a reason. The
caller keeps going with local refs, and never records an infrastructure failure as an application
defect.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from core.shared import netguard
from core.shared.config import Config, current

STATUS_OK = "ok"
STATUS_DISABLED = "disabled"
STATUS_REFUSED = "refused"
STATUS_UNAVAILABLE = "unavailable"

_MAX_PAGES = 20
_PAGE_SIZE = 100


def _result(status: str, reason: str, *, host: str | None = None,
            project: str | None = None) -> dict[str, Any]:
    return {"status": status, "reason": reason, "host": host, "project": project,
            "merge_request": None,
            "diff": {"fetched": False, "file_count": 0, "files": [], "truncated": False}}


def _config(cfg: Config) -> tuple[dict[str, Any], str | None]:
    """The section plus a refusal reason, checked before the token or any socket is touched."""
    section = cfg.section("gitlab") or {}
    if not section.get("enabled"):
        return section, "disabled: gitlab.enabled is false"
    host = str(section.get("host") or "").strip()
    if not host:
        return section, "disabled: gitlab.host is empty"
    parts = urlsplit(host)
    if parts.scheme not in ("http", "https"):
        return section, "refused: gitlab.host must be an http(s) URL"
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return section, ("refused: gitlab.host must be an origin without a path, query or "
                         "fragment")
    if not parts.hostname:
        return section, "refused: gitlab.host has no hostname"
    if not str(section.get("project") or "").strip():
        return section, "disabled: gitlab.project is empty"
    allowed = [str(item) for item in (cfg.get("api", "allowed_hosts", []) or [])]
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if not any(netguard.host_matches(entry, netguard.normalize_host(parts.hostname), port)
               for entry in allowed):
        return section, ("refused: gitlab.host is not in api.allowed_hosts; add "
                         f"{netguard.normalize_host(parts.hostname)}:{port} deliberately")
    return section, None


def _timeout(cfg: Config, section: dict[str, Any]) -> float:
    requested = float(section.get("timeout_seconds", 10) or 10)
    cap = float(cfg.get("api", "max_timeout_seconds", 120))
    return max(0.5, min(requested, cap))


def _header_value(section: dict[str, Any], token: str) -> dict[str, str]:
    header = str(section.get("token_header") or "PRIVATE-TOKEN")
    return {header: token, "Accept": "application/json"}


class _Unavailable(RuntimeError):
    """A transport or API failure; the message never contains the token or a response body."""


def _get(client: httpx.Client, url: str, cfg: Config) -> httpx.Response:
    decision = netguard.check_target(url, cfg)
    if not decision.allowed:
        raise _Unavailable(decision.reason)
    response = client.get(url)
    if 300 <= response.status_code < 400:
        raise _Unavailable("the GitLab API answered with a redirect; redirects are refused so a "
                           "request cannot leave the allowlisted host")
    return response


def _api_error(response: httpx.Response) -> str:
    detail = {
        401: "authentication failed; check that the token is valid",
        403: "the token is not allowed to read this merge request",
        404: "the merge request or project was not found, or the token cannot see it",
    }.get(response.status_code, "the GitLab API returned an error")
    return f"HTTP {response.status_code}: {detail}"


def _metadata(data: dict[str, Any]) -> dict[str, Any]:
    author = data.get("author") if isinstance(data.get("author"), dict) else {}
    return {
        "iid": data.get("iid"),
        "title": str(data.get("title") or ""),
        "state": str(data.get("state") or ""),
        "draft": bool(data.get("draft") or data.get("work_in_progress")),
        "source_branch": str(data.get("source_branch") or ""),
        "target_branch": str(data.get("target_branch") or ""),
        "sha": str(data.get("sha") or ""),
        "web_url": str(data.get("web_url") or ""),
        "author": str(author.get("username") or ""),
        "changes_count": str(data.get("changes_count") or ""),
        "created_at": str(data.get("created_at") or ""),
        "updated_at": str(data.get("updated_at") or ""),
    }


def _diff_files(client: httpx.Client, base_path: str, cfg: Config,
                max_files: int) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    truncated = False
    for page in range(1, _MAX_PAGES + 1):
        url = f"{base_path}/diffs?per_page={_PAGE_SIZE}&page={page}"
        response = _get(client, url, cfg)
        if response.status_code != 200:
            raise _Unavailable(_api_error(response))
        try:
            items = response.json()
        except ValueError as exc:
            raise _Unavailable("the diff endpoint returned a non-JSON body") from exc
        if not isinstance(items, list):
            raise _Unavailable("the diff endpoint returned an unexpected JSON shape")
        for item in items:
            if not isinstance(item, dict):
                continue
            files.append({
                "old_path": str(item.get("old_path") or ""),
                "new_path": str(item.get("new_path") or ""),
                "new_file": bool(item.get("new_file")),
                "deleted_file": bool(item.get("deleted_file")),
                "renamed_file": bool(item.get("renamed_file")),
            })
            if len(files) >= max_files:
                truncated = True
                break
        if truncated or not response.headers.get("x-next-page"):
            break
    else:
        truncated = True
    return {"fetched": True, "file_count": len(files), "files": files, "truncated": truncated}


def fetch_merge_request(iid: int | str, *, project: str | None = None,
                        cfg: Config | None = None) -> dict[str, Any]:
    """Fetch one merge request's metadata and per-file diff identities, or say why not.

    This function never raises for a configuration, authentication or transport problem and never
    retries a failed request. The caller decides whether local refs are enough.
    """
    cfg = cfg or current()
    section, refusal = _config(cfg)
    if refusal:
        status = STATUS_DISABLED if refusal.startswith("disabled") else STATUS_REFUSED
        return _result(status, refusal)
    project = str(project or section.get("project") or "").strip()
    if not project:
        return _result(STATUS_DISABLED, "disabled: no GitLab project was configured")
    try:
        iid_value = int(str(iid).strip())
    except (TypeError, ValueError):
        return _result(STATUS_UNAVAILABLE, "the merge request identifier is not an integer",
                       host=str(section.get("host")), project=project)
    if iid_value <= 0:
        return _result(STATUS_UNAVAILABLE, "the merge request identifier must be positive",
                       host=str(section.get("host")), project=project)

    token_env = str(section.get("token_env") or "").strip()
    token = os.environ.get(token_env) if token_env else None
    if not token:
        return _result(STATUS_UNAVAILABLE,
                       f"token environment variable '{token_env}' is not set; local refs are "
                       f"used instead", host=str(section.get("host")), project=project)

    host = str(section.get("host")).rstrip("/")
    encoded = quote(project, safe="")
    base_path = f"/api/v4/projects/{encoded}/merge_requests/{iid_value}"
    max_files = max(1, int(section.get("max_diff_files", 500) or 500))
    result = _result(STATUS_OK, "fetched", host=host, project=project)
    try:
        with httpx.Client(base_url=host, headers=_header_value(section, token),
                          timeout=_timeout(cfg, section), follow_redirects=False) as client:
            response = _get(client, f"{host}{base_path}", cfg)
            if response.status_code != 200:
                raise _Unavailable(_api_error(response))
            try:
                payload = response.json()
            except ValueError as exc:
                raise _Unavailable("the metadata endpoint returned a non-JSON body") from exc
            if not isinstance(payload, dict):
                raise _Unavailable("the metadata endpoint returned an unexpected JSON shape")
            result["merge_request"] = _metadata(payload)
            result["diff"] = _diff_files(client, f"{host}{base_path}", cfg, max_files)
    except _Unavailable as exc:
        failed = _result(STATUS_UNAVAILABLE, str(exc), host=host, project=project)
        return failed
    except httpx.HTTPError as exc:
        return _result(STATUS_UNAVAILABLE, f"the GitLab API was unreachable "
                                           f"({type(exc).__name__}); local refs are used instead",
                       host=host, project=project)
    return result


def compare_local(result: dict[str, Any], local_refs: dict[str, Any]) -> dict[str, Any]:
    """Add local-vs-server facts to a successful fetch, without fetching anything.

    `local_refs` carries the resolved local branch names and commit SHAs. A missing or mismatched
    SHA is reported as such; KHUNKHAAR never fetches, so the local ref may simply be older.
    """
    if result.get("status") != STATUS_OK or not result.get("merge_request"):
        return result
    merge_request = result["merge_request"]
    local = {"head_sha": str(local_refs.get("head_sha") or ""),
             "base_sha": str(local_refs.get("base_sha") or ""),
             "source_ref": str(local_refs.get("source_ref") or ""),
             "target_ref": str(local_refs.get("target_ref") or ""),
             "server_sha_present_locally": None,
             "head_matches_server": None,
             "notes": []}
    server_sha = merge_request.get("sha") or ""
    if not server_sha:
        local["notes"].append("the server response did not include a head SHA")
    elif not local["head_sha"]:
        local["notes"].append("the local head could not be resolved, so it was not compared")
    else:
        local["server_sha_present_locally"] = bool(local_refs.get("server_sha_present"))
        local["head_matches_server"] = local["head_sha"] == server_sha
        if not local["head_matches_server"]:
            local["notes"].append(
                "the local head does not match the merge request's server SHA; the analysis "
                "describes the local ref, not necessarily the server's current state" if
                local_refs.get("server_sha_present") else
                "the merge request's server SHA is not present in this checkout; the analysis "
                "describes the local ref, not the server's head")
    result["local"] = local
    return result
