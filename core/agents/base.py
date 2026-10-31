"""Shared agent/probe contract.

A probe is the smallest executable unit of QA intelligence: it knows one invariant type, uses
only the mapped contract and the target, returns structured evidence and never declares its own
finding confirmed. Agents own probes; the independent verifier re-runs them.
"""

from __future__ import annotations

import asyncio
import json
import traceback
from dataclasses import dataclass, field
from typing import Any, Protocol

from core.execution.api import ApiClient
from core.execution.db import DatabaseReadError, PostgresReader, SqliteReader
from core.missions import spec as specmod
from core.shared import evidence as ev
from core.shared.config import current


@dataclass
class ProbeResult:
    violated: bool
    expected: str
    actual: str
    steps: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)
    infrastructure_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"violated": self.violated, "expected": self.expected, "actual": self.actual,
                "steps": self.steps, "evidence": self.evidence, "details": self.details,
                "infrastructure_error": self.infrastructure_error}


class Probe(Protocol):
    kind: str
    title: str
    category: str
    severity: str
    agent: str

    async def run(self, ctx: "ProbeContext") -> ProbeResult: ...


@dataclass
class ProbeContext:
    project_id: str
    target: str
    base_url: str
    app_map: Any
    spec_doc: dict[str, Any] | None
    ops: list[dict[str, Any]]
    users: dict[str, dict[str, Any]]
    db: Any = None
    budget: Any = None
    log: Any = None
    run_id: str = "phase2"
    mode: str = "controlled"
    _clients: dict[str, ApiClient] = field(default_factory=dict, repr=False)

    # ---- identities ---------------------------------------------------------
    @property
    def roles(self) -> list[str]:
        return list(self.users)

    @property
    def default_role(self) -> str:
        if not self.users:
            raise RuntimeError("no user identities are configured for this run")
        return next(iter(self.users))

    def require_users(self, count: int) -> list[str]:
        if len(self.users) < count:
            raise RuntimeError(f"this probe needs {count} user identities, {len(self.users)} configured")
        return list(self.users)[:count]

    def _login_operation(self) -> dict[str, Any] | None:
        return specmod.find_operation(self.ops, "POST", ("login",)) or \
            specmod.find_operation(self.ops, "POST", ("session",)) or \
            specmod.find_operation(self.ops, "POST", ("signin",))

    def login(self, role: str) -> ApiClient:
        credentials = self.users.get(role)
        if credentials is None:
            raise RuntimeError(f"no credentials configured for role '{role}'")
        client = ApiClient(self.base_url, budget=self.budget, log=self.log)
        if credentials.get("headers"):
            client.headers.update({str(k): str(v) for k, v in credentials["headers"].items()})
        operation = self._login_operation()
        if operation:
            payload = specmod.build_payload(
                self.spec_doc, operation,
                overrides={name: credentials.get(name, "")
                           for name in ("username", "email", "user", "password", "pass", "pwd")})
            response = client.request("POST", operation["path"], json_body=payload)
            if not response.ok:
                client.close()
                raise RuntimeError(f"login failed for role '{role}' with HTTP {response.status}")
            body = response.body if isinstance(response.body, dict) else {}
            for token_key in ("token", "access_token", "jwt"):
                if isinstance(body.get(token_key), str):
                    client.headers["Authorization"] = f"Bearer {body[token_key]}"
                    break
        else:
            # No JSON login endpoint: the credentials must provide headers (e.g. an API token).
            if not credentials.get("headers"):
                client.close()
                raise RuntimeError("no login endpoint discovered and no headers configured")
        return client

    def client(self, role: str) -> ApiClient:
        if role not in self._clients:
            self._clients[role] = self.login(role)
        return self._clients[role]

    def fresh_client(self, role: str) -> ApiClient:
        return self.login(role)

    def close(self) -> None:
        for client in self._clients.values():
            client.close()
        self._clients.clear()

    # ---- evidence -----------------------------------------------------------
    def save_evidence(self, name: str, payload: dict[str, Any]) -> str:
        folder = ev.evidence_root("phase2", self.run_id)
        path = ev.write_json(folder, ev.slugify(name, 80) + ".json", payload,
                             project_id=self.project_id, run_id=self.run_id, kind="probe")
        return ev.relative(path)

    async def run_probe(self, probe: Probe) -> ProbeResult:
        try:
            result = await probe.run(self)
        except Exception as exc:  # infrastructure failure must be visible, not a false bug
            if self.log:
                self.log.event("probe_infrastructure_error", probe=probe.kind,
                               error=f"{type(exc).__name__}: {exc}",
                               traceback=traceback.format_exc(limit=6))
            return ProbeResult(False, "", "", infrastructure_error=f"{type(exc).__name__}: {exc}")
        if result.infrastructure_error and self.log:
            self.log.event("probe_infrastructure_error", probe=probe.kind,
                           error=result.infrastructure_error)
        return result


def open_db(path: str | None, profile: str | None = None):
    if path:
        return SqliteReader(path)
    if profile:
        return PostgresReader(profile)
    return None


def db_count(db, table: str) -> int | None:
    if db is None:
        return None
    try:
        value = db.scalar(f'SELECT count(*) FROM "{table}"')
        return int(value) if value is not None else None
    except (DatabaseReadError, Exception):
        return None


def guess_table(app_map, feature: str) -> str | None:
    candidates = []
    for entity in getattr(app_map, "entities", []):
        name = entity["name"].lower()
        if feature.rstrip("s") in name or name.startswith(feature[:5]):
            candidates.append(entity["name"])
    if candidates:
        return candidates[0]
    return None


def text_keywords(text: str, limit: int = 6) -> list[str]:
    words = [w.strip(".,;:()[]'\"!?*/").lower() for w in (text or "").split()]
    stop = {"must", "should", "shall", "only", "with", "from", "that", "this", "have", "cannot",
            "never", "more", "than", "into", "when", "after", "before", "each", "their", "other",
            "user's", "user", "users", "the", "and", "for", "not", "are", "may", "can", "one"}
    out = []
    for word in words:
        if len(word) < 4 or word in stop or word in out:
            continue
        out.append(word)
        if len(out) >= limit:
            break
    return out


def json_body(response) -> Any:
    return response.body if isinstance(response.body, (dict, list)) else {}


def body_contains(body: Any, needle: str) -> bool:
    return needle in json.dumps(body, default=str)


def run_async(coro):
    """Small helper for sync tests that need to invoke an async probe."""
    return asyncio.run(coro)
