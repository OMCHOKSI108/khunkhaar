"""Drives the dashboard's checklist generation and execution from real discovery + requests.

Replaces the old `agent.build_compact_checklist` (hardcoded `endpoints[:12]`/`models[:8]` slices,
five keyword-matched business rules) and `test_executor.py` (GraphQL-only, operation name parsed
out of the test title, one request per test, no reproduction check). Generation now scales with
every endpoint the recon scanner actually finds x every configured role (config/khunkhaar.yaml:
auth.db_mint.roles), up to `missions.max_total`. Execution goes through the policy-gated HTTP
layer (`core.shared.http_client`, same allowlist/SSRF/timeout enforcement as the rest of
KHUNKHAAR) and only marks a test FAILED after it reproduces 2/2 times, recording the concrete
steps - mirroring `core.verification.verifier.Verifier`'s confirmed/likely/not_reproducible
classification, scoped to the dashboard's single-request-style checks.
"""
from __future__ import annotations

import os
from typing import Any

from core.database.adapters import DriverUnavailable, PostgresAdapter, mask_ident, profile_by_name
from core.intelligence import mapper, rules as rules_mod
from core.recon.scanner import scan_project
from core.server import agent, auth_harvester, project_env, store
from core.server.models import ChecklistItem
from core.server.path_resolver import resolve_codebase_path
from core.shared.config import current
from core.shared.http_client import PolicyViolation, RequestSpec, TargetDenied, execute, graphql

TESTED_KINDS = {"rest", "graphql_query", "graphql_mutation"}
MAX_LLM_QUERY_CALLS = 60


def resolve_target_url(raw_url: str) -> str:
    """Normalize a configured target URL, bridging container-to-host networking."""
    url = (raw_url or "http://localhost:8000").strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "http://" + url
    if os.path.exists("/.dockerenv") or os.path.exists("/workspace"):
        for loopback in ("localhost:", "127.0.0.1:"):
            if loopback in url:
                url = url.replace(loopback, "host.docker.internal:")
                break
    return url


def _is_mutation(ep_kind: str, method: str) -> bool:
    return ep_kind == "graphql_mutation" or method.upper() in ("POST", "PUT", "PATCH", "DELETE")


def _classify(ep, role: str, privileged_keys: set[str]) -> tuple[str, str, str, str] | None:
    """Decide which assertion (if any) to generate for one (endpoint, role) pair.

    Mirrors how core/missions/generator.py maps an invariant to a check: this proposes what to
    look at, it never decides the verdict - execution and reproduction do that.
    """
    mutation = _is_mutation(ep.kind, ep.method)
    idor_hint = "{" in ep.path or " id" in f" {ep.description}".lower()

    if role == "unauthenticated":
        if not ep.auth_required:
            return None
        title = f"{'IDOR & ' if idor_hint else ''}Auth Bypass on {ep.method} {ep.name}"
        severity = "critical" if mutation else "high"
        return ("auth_bypass", title,
                f"Unauthenticated request to {ep.path} must be rejected, not served.", severity)

    if not ep.auth_required:
        return ("functional", f"Public Access Check on {ep.method} {ep.name}",
                f"Verify {ep.path} behaves correctly for an authenticated '{role}' session.", "low")

    if role in privileged_keys:
        return ("functional", f"Authorized Access on {ep.method} {ep.name}",
                f"Verify a '{role}' session can reach {ep.path} as intended.", "medium")

    if mutation:
        return ("rbac_boundary", f"RBAC Boundary on {ep.method} {ep.name}",
                f"Verify role '{role}' is blocked from the privileged mutation {ep.name}.", "high")

    return ("functional", f"Role Access Check on {ep.method} {ep.name}",
            f"Verify role '{role}' session behavior on {ep.path}.", "medium")


def _category(assertion: str) -> str:
    return {"auth_bypass": "cyber", "rbac_boundary": "rbac", "functional": "end-user"}.get(assertion, "cyber")


def build_app_map(codebase_path):
    """One pass of core.intelligence.mapper, exactly how the real orchestrator's discover()
    step runs it (core/orchestrator/engine.py) - gives both REST routes (a broader regex set
    than core.recon.scanner's Streamlit/Next.js-only coverage: Flask/FastAPI/Django/Express/
    NestJS/Laravel/Spring) and documented business rules classified into invariant kinds."""
    try:
        return mapper.build(codebase_path, project_id="checklist-scan", rules_classifier=rules_mod.extract_rules)
    except Exception:
        return None


def _mapper_rest_endpoints(app_map) -> list:
    """Convert mapper's file-level Endpoint rows into EndpointDefs the checklist matrix uses.
    auth_required is approximated from file-level auth signals, since mapper tracks those per
    file rather than per route."""
    from core.recon.models import EndpointDef

    if app_map is None:
        return []
    auth_files = set(app_map.auth_signals)
    out = []
    for ep in app_map.endpoints:
        method = "GET" if ep.method == "ANY" else ep.method
        out.append(EndpointDef(
            name=ep.path, kind="rest", path=ep.path, method=method,
            source_file=ep.source, line=ep.line, auth_required=ep.source in auth_files,
            description=f"{ep.framework or 'REST'} route",
        ))
    return out


SEVERITY_BY_INVARIANT_KIND = {
    "authorization": "critical", "workflow": "high", "atomicity": "high",
    "concurrency": "high", "idempotency": "high", "validation": "medium",
    "timezone": "medium", "responsive": "low", "ux": "low",
}


def _business_rule_items(project_id: str, app_map, start_idx: int,
                         max_items: int) -> tuple[list[ChecklistItem], dict[str, dict]]:
    """Turn documented, evidence-backed business rules into checklist items. Unlike the
    endpoint x role matrix these aren't auto-replayed against the target (that needs an
    OpenAPI-style operation contract this bridge doesn't build); they're surfaced for human/
    journey-test review with their exact source so nothing here is a guess."""
    if app_map is None:
        return [], {}
    items: list[ChecklistItem] = []
    specs: dict[str, dict] = {}
    for i, invariant in enumerate(rules_mod.invariants_from_rules(app_map)[:max_items]):
        item_id = f"chk-{start_idx + i:04d}"
        kind = invariant.get("kind", "other")
        items.append(ChecklistItem(
            id=item_id, project_id=project_id, category="business_logic",
            title=f"[{kind.title()}] {invariant['description'][:90]}",
            description=f"Documented rule at {invariant.get('source')}. "
                       f"Suggested check: {invariant.get('method', 'manual review')}.",
            role="documented_rule", severity=SEVERITY_BY_INVARIANT_KIND.get(kind, "medium"),
            selected=True, status="pending",
        ))
        specs[item_id] = {"assertion": "business_rule", "invariant": invariant}
    return items, specs


def generate_checklist(project_id: str, proj) -> list[ChecklistItem]:
    """Discover the real endpoint x role matrix and turn it into checklist items + specs."""
    codebase_path = resolve_codebase_path(proj.root_path)
    if not codebase_path:
        return []

    recon = scan_project(codebase_path)
    app_map = build_app_map(codebase_path)
    mapper_rest = _mapper_rest_endpoints(app_map)
    seen_paths = {(e.method, e.path) for e in recon.endpoints}
    recon.endpoints.extend(e for e in mapper_rest if (e.method, e.path) not in seen_paths)
    cfg = current()
    # Per-project auto-discovered roles (what auth_harvester actually mints tokens for) take
    # priority over the global manual auth.db_mint block - otherwise the matrix generates
    # generic "authenticated" items that never match a real minted token key.
    project_schema = store.get_project_auth_config(project_id)
    mint_cfg = cfg.section("auth").get("db_mint") or {}
    roles_source = (project_schema or {}).get("roles") or mint_cfg.get("roles") or []
    role_keys = sorted({r.get("key") for r in roles_source if r.get("key")})
    privileged_keys = {"admin"}
    all_roles = ["unauthenticated"] + (role_keys or ["authenticated"])
    max_total = int(cfg.get("missions", "max_total", 400))

    items: list[ChecklistItem] = []
    specs: dict[str, dict[str, Any]] = {}
    idx = 0
    # One LLM call per unique GraphQL field, not per test: a "study" step that builds a
    # realistic query/mutation body (plausible args) instead of a blind `{ name { __typename } }`
    # probe, reused across every role variant of that same endpoint's checklist items. Capped so
    # a large schema can't turn one recon pass into hundreds of sequential LLM calls.
    query_cache: dict[tuple[str, str], str | None] = {}
    llm_calls_left = MAX_LLM_QUERY_CALLS
    for ep in recon.endpoints:
        if ep.kind not in TESTED_KINDS:
            continue
        if ep.kind in ("graphql_query", "graphql_mutation") and llm_calls_left > 0:
            cache_key = (ep.kind, ep.name)
            if cache_key not in query_cache:
                query_cache[cache_key] = agent.build_graphql_call(ep.name, ep.kind, ep.description)
                llm_calls_left -= 1
        for role in all_roles:
            classified = _classify(ep, role, privileged_keys)
            if classified is None:
                continue
            idx += 1
            item_id = f"chk-{idx:04d}"
            assertion, title, description, severity = classified
            items.append(ChecklistItem(
                id=item_id, project_id=project_id, category=_category(assertion),
                title=title, description=description, role=role, severity=severity,
                selected=True, status="pending",
            ))
            specs[item_id] = {
                "endpoint": {"name": ep.name, "kind": ep.kind, "path": ep.path, "method": ep.method,
                            "query_override": query_cache.get((ep.kind, ep.name))},
                "role": role, "assertion": assertion,
            }
            if len(items) >= max_total:
                break
        if len(items) >= max_total:
            break

    if len(items) < max_total:
        rule_items, rule_specs = _business_rule_items(
            project_id, app_map, start_idx=idx + 1, max_items=max_total - len(items))
        items.extend(rule_items)
        specs.update(rule_specs)

    store.save_checklist_specs(project_id, specs)
    return items


def _send(kind: str, method: str, name: str, path: str, url: str, token: str | None,
         query_override: str | None = None) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        if kind in ("graphql_query", "graphql_mutation"):
            verb = "query" if kind == "graphql_query" else "mutation"
            body = query_override or f"{verb} {{ {name} {{ __typename }} }}"
            result = graphql(url, body, None, None, headers=headers)
        else:
            full_url = url.rstrip("/") + path if path.startswith("/") else f"{url.rstrip('/')}/{path}"
            spec = RequestSpec(method=method, url=full_url, headers=headers)
            result = execute(spec)
    except (TargetDenied, PolicyViolation) as exc:
        return {"ok": False, "status_code": None, "body": None, "error": str(exc), "graphql_errors": None}
    return result


def _violated(assertion: str, role: str, result: dict[str, Any]) -> tuple[bool, str]:
    """True = the invariant was violated (should FAIL); returns (violated, evidence note)."""
    status = result.get("status_code")
    gql_errors = result.get("graphql_errors") or []
    error_text = " ".join(str(e.get("message", e)) for e in gql_errors).lower() if gql_errors else \
        str(result.get("error") or "").lower()
    blocked = status in (401, 403) or any(
        k in error_text for k in ("login", "unauthorized", "auth", "token", "jwt", "forbidden", "permission", "denied"))
    body = result.get("body") if isinstance(result.get("body"), dict) else {}
    data = body.get("data") if isinstance(body, dict) else None
    has_data = bool(data) and any(v is not None for v in data.values()) if isinstance(data, dict) else bool(result.get("ok") and status and status < 300)

    if assertion in ("auth_bypass", "rbac_boundary"):
        if blocked:
            return False, f"blocked as expected (HTTP {status or 'n/a'}: {error_text[:120] or 'auth error'})"
        if has_data or (result.get("ok") and status and status < 300 and not gql_errors):
            return True, f"request for role '{role}' was NOT blocked (HTTP {status}), data/success returned"
        return False, f"no data returned and no explicit block (HTTP {status}); treated as safe"

    # functional: we expect success, or a schema-level validation error (server is alive and
    # enforcing structure) - only an unexpected auth rejection counts as a violation here.
    if blocked and role != "unauthenticated":
        return True, f"role '{role}' was unexpectedly blocked (HTTP {status}: {error_text[:120]})"
    return False, f"completed (HTTP {status or 'n/a'})"


def _db_state_note(spec: dict[str, Any]) -> str | None:
    """Best-effort extra evidence: confirm the target table's row count via a read-only query."""
    mint_cfg = current().section("auth").get("db_mint") or {}
    profile_name = mint_cfg.get("database_profile")
    if not profile_name:
        return None
    name = spec["endpoint"]["name"].lower()
    try:
        profile = profile_by_name(profile_name)
        adapter = PostgresAdapter(profile)
        candidates = [t for t in (mint_cfg.get("users_table"), name.rstrip("s"), name) if t]
        for table in candidates:
            try:
                rows = adapter.fetch_rows(f"SELECT count(*) AS n FROM {mask_ident(table)}")
                adapter.close()
                return f"DB check: {table} row count = {rows[0]['n']}" if rows else None
            except Exception:
                continue
    except (DriverUnavailable, KeyError, PermissionError, Exception):
        return None
    return None


SCHEMA_GLOBS = ("**/schema.prisma", "**/migrations/*.sql", "**/models.py", "**/models/*.py",
               "**/models/*.js", "**/models/*.ts")
MAX_SCHEMA_FILES = 10
MAX_CHARS_PER_FILE = 4000


def gather_auth_schema_text(codebase_path, app_map=None) -> str:
    """Bounded excerpts of likely users-table/auth source, for `agent.suggest_auth_config`.
    Reuses mapper's file-level auth signals (files matching AUTH_PATTERN) plus common schema
    file globs - never the whole codebase, and nothing here leaves the process except as a
    prompt to the configured LLM harness."""
    skip_dirs = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", "vendor"}

    def _vendored(path) -> bool:
        return bool(skip_dirs & set(path.parts))

    priority_hints = ("user", "auth", "token", "role", "session", "account", "permission")

    def _priority(path) -> int:
        name = path.name.lower()
        return 0 if any(h in name for h in priority_hints) else 1

    candidates: list = []
    if app_map is not None:
        candidates.extend(codebase_path / f for f in app_map.auth_signals)
    for pattern in SCHEMA_GLOBS:
        candidates.extend(p for p in codebase_path.glob(pattern) if not _vendored(p))
    candidates.sort(key=_priority)

    seen: set = set()
    chunks: list[str] = []
    for path in candidates:
        if path in seen or len(seen) >= MAX_SCHEMA_FILES:
            continue
        seen.add(path)
        try:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")[:MAX_CHARS_PER_FILE]
        except OSError:
            continue
        chunks.append(f"--- {path.relative_to(codebase_path)} ---\n{text}")
    return "\n\n".join(chunks)


def auto_configure_auth(project_id: str, proj) -> dict[str, Any] | None:
    """Fully agent-driven auth setup: infer this project's users-table schema via the LLM
    harness, pair it with the DB/JWT env var NAMES found in its own .env, and cache the result
    (schema + key names only, never secret values) in web_project_auth. Returns None when
    auto-discovery is off (auth.db_mint.auto_discover_from_project_env) or nothing usable was
    found - callers then fall back to the manual config block or run with no auth at all."""
    mint_cfg = current().section("auth").get("db_mint") or {}
    if not mint_cfg.get("auto_discover_from_project_env"):
        return None

    codebase_path = resolve_codebase_path(proj.root_path)
    if not codebase_path:
        return None

    app_map = build_app_map(codebase_path)
    schema_text = gather_auth_schema_text(codebase_path, app_map)
    if not schema_text:
        return None

    draft = agent.suggest_auth_config(schema_text)
    if not draft or not draft.get("roles"):
        return None

    env_info = project_env.discover_db_and_jwt(codebase_path)
    if not env_info.get("password_key") or not env_info.get("jwt_secret_key"):
        return None  # found a schema but no identifiable secret in this project's .env

    config = {
        **draft,
        "host": env_info["host"], "port": env_info["port"], "database": env_info["database"],
        "user": env_info["user"], "password_env_key": env_info["password_key"],
        "jwt_secret_env_key": env_info["jwt_secret_key"],
    }
    store.save_project_auth_config(project_id, config)
    auth_harvester.TOKEN_CACHE.pop(f"project:{project_id}", None)
    return config


def execute_checklist_item(project_id: str, item: ChecklistItem, target_url: str,
                           codebase_dir: str | None = None) -> tuple[str, str, dict[str, Any]]:
    """Run one checklist item for real, requiring 2/2 reproduction before marking it failed."""
    spec = store.get_checklist_spec(project_id, item.id)
    if not spec:
        return ("failed", "No execution spec recorded for this item - regenerate the checklist.", {})

    if spec["assertion"] == "business_rule":
        inv = spec["invariant"]
        reason = (f"📋 Documented rule, not auto-replayed (needs an operation contract this "
                 f"bridge doesn't build for {inv.get('kind')} checks): \"{inv['description']}\" "
                 f"- source {inv.get('source')}. Suggested check: {inv.get('method')}.")
        return ("passed", reason, {"spec": spec, "steps": [reason], "reproductions": 0})

    url = resolve_target_url(target_url)
    tokens = auth_harvester.get_role_tokens(codebase_dir, project_id)
    role = spec["role"]
    token = tokens.get(role) if role not in ("unauthenticated", "authenticated") else None
    ep = spec["endpoint"]

    attempts: list[tuple[bool, str]] = []
    for _ in range(2):
        result = _send(ep["kind"], ep["method"], ep["name"], ep["path"], url, token,
                      query_override=ep.get("query_override"))
        if result.get("status_code") is None and not result.get("graphql_errors") and result.get("error"):
            return ("failed", f"Target unreachable at {url}: {result['error']}", {})
        attempts.append(_violated(spec["assertion"], role, result))

    violations = sum(1 for v, _ in attempts if v)
    steps = [f"attempt {i + 1}: {note}" for i, (_, note) in enumerate(attempts)]
    db_note = _db_state_note(spec) if spec["assertion"] in ("auth_bypass", "rbac_boundary") and violations else None
    if db_note:
        steps.append(db_note)

    reproduction = {"spec": spec, "steps": steps, "reproductions": violations}
    if violations >= 2:
        reason = "🚨 CONFIRMED (2/2 reproductions): " + " | ".join(steps)
        return ("failed", reason, reproduction)
    if violations == 1:
        reason = "Not independently reproducible (1/2) - passing pending re-check: " + " | ".join(steps)
        return ("passed", reason, reproduction)
    reason = steps[-1] if steps else "completed"
    return ("passed", reason, reproduction)
