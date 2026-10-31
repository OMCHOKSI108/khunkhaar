"""Bounded HTTP client used by Phase 2 agents.

Unlike the MCP API tool (one-shot requests), agents need a cookie-carrying session to simulate
several users and to replay workflows. Safety stays identical: every request passes the Phase 1
netguard allowlist, destructive methods keep the double gate, bodies are redacted and every call
is counted against the run budget.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from core.shared import netguard
from core.shared.config import Config, current
from core.shared.redaction import redact_headers, redact_text

MAX_BODY_CHARS = 20_000


@dataclass
class ApiResponse:
    status: int | None
    url: str
    body: Any
    headers: dict[str, str] = field(default_factory=dict)
    duration_ms: float = 0.0
    error: str | None = None
    error_kind: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.status is not None and 200 <= self.status < 400

    @property
    def status_class(self) -> str:
        return f"{self.status // 100}xx" if self.status else "none"

    def json(self) -> Any:
        return self.body

    def text(self) -> str:
        return self.body if isinstance(self.body, str) else str(self.body)

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "url": self.url, "body": self.body,
                "headers": self.headers, "duration_ms": self.duration_ms,
                "error": self.error, "error_kind": self.error_kind}


class ApiClient:
    def __init__(self, base_url: str, *, cfg: Config | None = None, budget=None, log=None,
                 headers: dict[str, str] | None = None, allow_destructive: bool = False,
                 timeout: float = 10.0):
        self.base_url = base_url.rstrip("/") + "/"
        self.cfg = cfg or current()
        self.budget = budget
        self.log = log
        self.headers = dict(headers or {})
        self.allow_destructive = allow_destructive
        self.timeout = timeout
        self._client = httpx.Client(timeout=timeout, follow_redirects=False)

    # ---- lifecycle ----------------------------------------------------------
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ApiClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def clone(self, *, headers: dict[str, str] | None = None) -> "ApiClient":
        """Fresh client with the same identity. Used by verification to avoid shared state."""
        other = ApiClient(self.base_url, cfg=self.cfg, budget=self.budget, log=self.log,
                          headers={**self.headers, **(headers or {})},
                          allow_destructive=self.allow_destructive, timeout=self.timeout)
        other._client.cookies.update(self._client.cookies)
        return other

    @property
    def cookies(self) -> dict[str, str]:
        return dict(self._client.cookies)

    def set_cookie(self, name: str, value: str) -> None:
        self._client.cookies.set(name, value)

    # ---- requests -----------------------------------------------------------
    def _url(self, path: str) -> str:
        return path if path.startswith("http") else urljoin(self.base_url, path.lstrip("/"))

    def _authorize(self, url: str, method: str) -> None:
        decision = netguard.check_target(url, self.cfg)
        if not decision.allowed:
            raise PermissionError(f"target refused by allowlist: {decision.reason}")
        destructive = [m.upper() for m in self.cfg.get("api", "destructive_methods", [])]
        if method.upper() in destructive:
            if not self.allow_destructive:
                raise PermissionError("destructive method blocked (allow_destructive is false)")
            host = netguard.normalize_host(urlsplit(url).hostname or "")
            port = urlsplit(url).port
            approved = [str(h) for h in (self.cfg.get("security", "destructive_approved_targets", []) or [])]
            if not any(netguard.host_matches(entry, host, port) for entry in approved):
                raise PermissionError(f"destructive method not approved for host {host}")

    def request(self, method: str, path: str, *, json_body: Any = None,
                data: dict[str, Any] | None = None, headers: dict[str, str] | None = None,
                params: dict[str, Any] | None = None,
                timeout: float | None = None, budget_count: int = 1) -> ApiResponse:
        method = method.upper()
        url = self._url(path)
        self._authorize(url, method)
        if self.budget:
            self.budget.consume_requests(budget_count)
        started = time.monotonic()
        merged = {**self.headers, **(headers or {})}
        try:
            response = self._client.request(method, url, json=json_body, data=data,
                                            params=params, headers=merged,
                                            timeout=timeout or self.timeout)
        except httpx.HTTPError as exc:
            elapsed = round((time.monotonic() - started) * 1000, 2)
            result = ApiResponse(None, url, None, {}, elapsed,
                                 redact_text(f"{type(exc).__name__}: {exc}"), "transport")
            self._log(method, url, result)
            return result

        body, kind = self._decode(response)
        result = ApiResponse(
            response.status_code, str(response.url), body,
            redact_headers(response.headers), round((time.monotonic() - started) * 1000, 2),
        )
        result.headers["_body_kind"] = kind
        self._log(method, url, result)
        return result

    def _decode(self, response: httpx.Response) -> tuple[Any, str]:
        # The body stays unredacted in memory: agents need real values (session tokens, ids,
        # field names) to execute and verify. Every persisted artifact is redacted on write,
        # and this client never logs bodies.
        text = response.text[:MAX_BODY_CHARS]
        ctype = response.headers.get("content-type", "")
        if "json" in ctype.lower() or text[:1] in "[{":
            try:
                return response.json(), "json"
            except ValueError:
                pass
        return text, "text"

    def _log(self, method: str, url: str, result: ApiResponse) -> None:
        if self.log:
            self.log.event("api_call", method=method, url=url, status=result.status,
                           duration_ms=result.duration_ms, error=result.error)

    def get(self, path: str, **kwargs: Any) -> ApiResponse:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> ApiResponse:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> ApiResponse:
        return self.request("PUT", path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> ApiResponse:
        return self.request("PATCH", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> ApiResponse:
        return self.request("DELETE", path, **kwargs)

    def parallel(self, method: str, path: str, payloads: list[dict[str, Any]],
                 max_workers: int = 5) -> list[ApiResponse]:
        """Fire the same request with several bodies concurrently, bounded and budgeted.

        Each worker gets its own connection and a copy of the session cookies, so the server
        sees independent requests from the same authenticated user - the setup a race-condition
        probe needs.
        """
        from concurrent.futures import ThreadPoolExecutor

        workers = max(1, min(max_workers, len(payloads)))
        if self.budget:
            self.budget.consume_requests(len(payloads))

        def worker(payload: dict[str, Any]) -> ApiResponse:
            url = self._url(path)
            self._authorize(url, method)
            with httpx.Client(timeout=self.timeout, follow_redirects=False,
                              cookies=self._client.cookies) as client:
                started = time.monotonic()
                try:
                    response = client.request(method.upper(), url, json=payload,
                                              headers=self.headers)
                except httpx.HTTPError as exc:
                    return ApiResponse(None, url, None, {}, 0.0, str(exc), "transport")
                body, _ = self._decode(response)
                return ApiResponse(response.status_code, str(response.url), body,
                                   redact_headers(response.headers),
                                   round((time.monotonic() - started) * 1000, 2))

        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(worker, payloads))
