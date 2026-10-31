"""Execute a generated test intent against a live target.

Generated tests import this module instead of reimplementing journey or probe mechanics. Every
failure mode is classified: a missing target, missing credentials, an absent API contract or a
transport error becomes `infrastructure_error` and the test skips, so an unavailable environment
can never be mistaken for a reproduced defect. No credential is written to an artifact or printed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from core.agents.base import ProbeContext, open_db
from core.agents.probes import PROBE_BY_KIND
from core.findings import store as findings_store
from core.intelligence import mapper
from core.journeys import models as journeys
from core.journeys.runner import run_mutation
from core.missions import spec as specmod
from core.shared import paths


def execute(intent: dict[str, Any], *, base_url: str = "", users_path: str | None = None,
            db_path: str | None = None, app_map: Any = None, ops: list[dict[str, Any]] | None = None,
            spec_doc: dict[str, Any] | None = None) -> dict[str, Any]:
    expected = str(intent.get("expected") or "")
    if not base_url:
        return _failed(expected, "KHUNKHAAR_TARGET is not set, so the test did not contact "
                                 "the application")
    try:
        users = load_users(users_path)
        if intent.get("method") == "journey":
            return _run_journey(intent, base_url, users, db_path)
        return _run_probe(intent, base_url, users, db_path, app_map=app_map, ops=ops,
                          spec_doc=spec_doc)
    except Exception as exc:  # the caller turns this into a pytest skip, never an assertion
        return _failed(expected, f"{type(exc).__name__}: {exc}")


def load_users(users_path: str | None) -> dict[str, dict[str, Any]]:
    if not users_path:
        return {}
    path = Path(users_path).expanduser()
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("users file must map role -> {username,password} or {headers}")
    return {str(role): dict(value) for role, value in document.items()
            if isinstance(value, dict)}


def _context(intent: dict[str, Any], base_url: str, users: dict[str, dict[str, Any]],
             db_path: str | None, *, app_map: Any = None, ops: list | None = None,
             spec_doc: dict | None = None) -> ProbeContext:
    db = open_db(db_path) if db_path else None
    return ProbeContext(
        project_id=str(intent.get("project_id") or ""), target=base_url, base_url=base_url,
        app_map=app_map, spec_doc=spec_doc, ops=list(ops or []), users=users, db=db,
        mode="regression",
    )


def _run_journey(intent: dict[str, Any], base_url: str, users: dict[str, dict[str, Any]],
                 db_path: str | None) -> dict[str, Any]:
    reproduction = intent.get("reproduction") or {}
    journey_doc = reproduction.get("journey")
    mutation_doc = reproduction.get("mutation")
    expected = str(intent.get("expected") or "")
    if not isinstance(journey_doc, dict) or not isinstance(mutation_doc, dict):
        return _failed(expected, "the captured journey transcript is unavailable; regenerate "
                                 "the test after a verification that saved it")
    journey = journeys.Journey(
        name=str(journey_doc.get("name") or intent.get("feature") or "regression"),
        feature=str(journey_doc.get("feature") or intent.get("feature") or "application"),
        steps=[_step(item) for item in journey_doc.get("steps") or []],
        journey_id=str(journey_doc.get("journey_id") or "regression"),
    )
    mutation = journeys.Mutation(
        name=str(mutation_doc.get("name") or "regression"),
        description=str(mutation_doc.get("description") or ""),
        steps=[_step(item) for item in mutation_doc.get("steps") or []],
        expectation=str(mutation_doc.get("expectation") or expected),
        meaningful=bool(mutation_doc.get("meaningful", True)),
    )
    ctx = _context(intent, base_url, users, db_path)
    try:
        outcome = asyncio.run(run_mutation(ctx, journey, mutation))
    finally:
        ctx.close()
    return {
        "violated": bool(outcome.violated),
        "expected": outcome.expected or expected,
        "actual": outcome.actual,
        "steps": [f"{item['method']} {item['path']} -> {item['status']}"
                  for item in outcome.executed],
        "evidence": outcome.evidence,
        "infrastructure_error": outcome.infrastructure_error,
    }


def _step(item: dict[str, Any]) -> journeys.JourneyStep:
    return journeys.JourneyStep(
        name=str(item.get("name") or "step"), method=str(item.get("method") or "GET"),
        path=str(item.get("path") or "/"), payload=item.get("payload"),
        actor=item.get("actor"), save_as=item.get("save_as"),
        expect=str(item.get("expect") or "2xx"),
    )


def _run_probe(intent: dict[str, Any], base_url: str, users: dict[str, dict[str, Any]],
               db_path: str | None, *, app_map: Any, ops: list | None,
               spec_doc: dict | None) -> dict[str, Any]:
    reproduction = intent.get("reproduction") or {}
    invariant = reproduction.get("invariant") or {}
    kind = str(reproduction.get("probe_kind") or invariant.get("kind") or "")
    probe_cls = PROBE_BY_KIND.get(kind)
    expected = str(intent.get("expected") or "")
    if probe_cls is None:
        return _failed(expected, f"no probe is available for kind '{kind}'")
    if not ops:
        document = spec_doc if spec_doc is not None else _fetch_contract(base_url)
        spec_doc = document
        ops = specmod.operations(document)
    if not ops:
        return _failed(expected, "no OpenAPI contract is available from the target, so the "
                                 "probe cannot rebuild its setup operations")
    if app_map is None:
        app_map = _load_app_map(str(intent.get("project_id") or ""))
    ctx = _context(intent, base_url, users, db_path, app_map=app_map, ops=ops,
                   spec_doc=spec_doc)
    try:
        result = asyncio.run(ctx.run_probe(probe_cls(invariant)))
    finally:
        ctx.close()
    return {
        "violated": bool(result.violated),
        "expected": result.expected or expected,
        "actual": result.actual,
        "steps": result.steps,
        "evidence": result.evidence,
        "infrastructure_error": result.infrastructure_error,
    }


def _fetch_contract(base_url: str) -> dict[str, Any] | None:
    from core.execution.api import ApiClient

    client = ApiClient(base_url)
    try:
        return specmod.fetch_spec(client)
    finally:
        client.close()


def _load_app_map(project_id: str) -> Any:
    row = findings_store.latest_app_map(project_id) if project_id else None
    if not row:
        return None
    candidate = Path(str(row.get("path") or ""))
    if not candidate.is_absolute():
        candidate = paths.kh_root() / candidate
    try:
        data = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    endpoints = [mapper.Endpoint(**{key: item.get(key) for key in
                                    ("method", "path", "source", "line", "framework")})
                 for item in data.get("endpoints") or [] if isinstance(item, dict)]
    return mapper.ApplicationMap(
        project_id=str(data.get("project_id") or project_id),
        root=str(data.get("root") or ""),
        endpoints=endpoints,
        entities=list(data.get("entities") or []),
        frameworks=list(data.get("frameworks") or []),
        auth_signals=list(data.get("auth_signals") or []),
        api_specs=list(data.get("api_specs") or []),
        docs=list(data.get("docs") or []),
        tests=list(data.get("tests") or []),
        files_scanned=int(data.get("files_scanned") or 0),
        truncated=bool(data.get("truncated")),
    )


def _failed(expected: str, reason: str) -> dict[str, Any]:
    return {"violated": False, "expected": expected, "actual": "", "steps": [],
            "evidence": {}, "infrastructure_error": reason}
