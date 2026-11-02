"""Token-optimized QA Agent interface for Khunkhaar Web Server."""
from __future__ import annotations

import json
from typing import Any
from core.llm.harness import get_harness, HarnessError

QA_SYSTEM_PROMPT = """You are KHUNKHAAR, an autonomous Senior QA Engineer and Security Auditor.
Rules:
- Be concise, direct, high-signal. Zero boilerplate or conversational filler.
- Focus on business logic leaks, multi-role RBAC flaws, IDOR, SQLi, and side-effects.
- When generating tests, produce precise steps to reproduce, HTTP method, path, and payload.
- Respond in direct markdown or structured JSON as requested."""

AUTH_INFER_SYSTEM_PROMPT = """You infer a QA tool's auth-bootstrap config from a codebase's \
schema/model/auth source. Output STRICT JSON only - no prose, no markdown fences - matching \
exactly this shape:
{"users_table": str, "id_column": str, "email_column": str, "role_column": str,
 "active_column": str|null, "claim_user_id": str, "roles": [{"value": str, "key": str}],
 "session_table": str|null, "session_user_fk": str|null, "session_token_column": str|null}
"value" is the role exactly as it appears in the source (e.g. an enum member or string literal).
"key" is a short lowercase snake_case tier you choose (reuse "admin" for the highest privilege
tier if one exists). Only include roles that literally appear in the provided source - never
invent one. If a field can't be determined, use a sensible default (users_table="users",
id_column="id") rather than omitting it. If no users table is identifiable at all, return every
field null/empty rather than guessing."""


def _call_llm(system: str, prompt: str, max_tokens: int = 1000) -> str:
    harness = get_harness()
    try:
        if harness.name == "cloudflare":
            return harness.chat_code(system, prompt, max_tokens=max_tokens)
        elif harness.name == "mistral":
            return harness.chat_with_fallback(system, prompt, max_tokens=max_tokens)
        return harness.chat(system, prompt, max_tokens=max_tokens)
    except Exception as exc:
        raise HarnessError(str(exc)) from exc


def ask_qa_agent(query: str, project_context: str = "") -> str:
    """Send a user query with optimized project context to the best available free LLM."""
    prompt = f"Project Context:\n{project_context}\n\nTask: {query}" if project_context else query
    try:
        resp = _call_llm(QA_SYSTEM_PROMPT, prompt, max_tokens=1000)
        return json.dumps(resp) if isinstance(resp, dict) else str(resp)
    except HarnessError as exc:
        return f"Agent offline: {exc}"


def suggest_auth_config(schema_text: str) -> dict[str, Any]:
    """Draft an `auth.db_mint` config block from schema/auth source excerpts.

    Never applied automatically - the caller reviews the draft before writing it into
    config/khunkhaar.yaml and flipping `enabled: true`. Returns {} on any failure (unavailable
    harness, unparseable response) rather than guessing.
    """
    if not schema_text.strip():
        return {}
    prompt = (f"Codebase schema/auth excerpts (each prefixed with its file path):\n\n"
             f"{schema_text[:12000]}\n\nReturn the JSON config described in your instructions.")
    try:
        raw = _call_llm(AUTH_INFER_SYSTEM_PROMPT, prompt, max_tokens=800)
    except HarnessError:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        start, end = raw.index("{"), raw.rindex("}") + 1
        parsed = json.loads(raw[start:end])
    except (ValueError, json.JSONDecodeError, AttributeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def build_compact_checklist(project_name: str, endpoints: list[dict], models: list[str],
                            context_notes: str = "") -> list[dict]:
    """Compatibility shim for tests; real generation is in core.server.engine_bridge."""
    items: list[dict] = []
    for ep in endpoints[:12]:
        name, path, method = ep.get("name", "ep"), ep.get("path", "/"), ep.get("method", "GET").upper()
        items.append({
            "category": "cyber", "title": f"IDOR & Auth Bypass on {method} {name}",
            "description": f"Test access on {path}", "role": "unauthenticated",
            "severity": "critical" if method in ("POST", "PUT", "DELETE") else "high",
        })
    for mod in models[:8]:
        items.append({
            "category": "rbac", "title": f"Multi-role Permission Boundary on {mod}",
            "description": f"Verify permissions on {mod}", "role": "employee", "severity": "high",
        })
    return items

