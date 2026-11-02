"""Token-optimized QA Agent interface for Khunkhaar Web Server."""
from __future__ import annotations

from typing import Any
from core.llm.harness import get_harness, HarnessError

QA_SYSTEM_PROMPT = """You are KHUNKHAAR, an autonomous Senior QA Engineer and Security Auditor.
Rules:
- Be concise, direct, high-signal. Zero boilerplate or conversational filler.
- Focus on business logic leaks, multi-role RBAC flaws, IDOR, SQLi, and side-effects.
- When generating tests, produce precise steps to reproduce, HTTP method, path, and payload.
- Respond in direct markdown or structured JSON as requested."""


def ask_qa_agent(query: str, project_context: str = "") -> str:
    """Send a user query with optimized project context to the best available free LLM."""
    harness = get_harness()
    prompt = f"Project Context:\n{project_context}\n\nTask: {query}" if project_context else query
    try:
        if harness.name == "cloudflare":
            return harness.chat_code(QA_SYSTEM_PROMPT, prompt, max_tokens=1000)
        elif harness.name == "mistral":
            return harness.chat_with_fallback(QA_SYSTEM_PROMPT, prompt, max_tokens=1000)
        return harness.chat(QA_SYSTEM_PROMPT, prompt, max_tokens=1000)
    except Exception as exc:
        return f"Agent offline: {exc}"


def _add_context_rules(notes: str, items: list[dict]) -> None:
    low = notes.lower()
    if any(k in low for k in ("attendance", "punch")):
        items.append({
            "category": "business", "title": "Attendance Geolocation & IP Tampering Prevention",
            "description": "Ensure attendance clock-ins cannot be spoofed across spoofed IPs or time offsets",
            "role": "employee", "severity": "high"
        })
    if any(k in low for k in ("leave", "vacation")):
        items.append({
            "category": "business", "title": "Concurrent Leave Request Race Condition & Balance Overdraw",
            "description": "Verify simultaneous leave submissions do not exceed allocated employee leave quotas",
            "role": "employee", "severity": "high"
        })
    if any(k in low for k in ("pod", "approval")):
        items.append({
            "category": "rbac", "title": "Pod Lead Circular Approval & Self-Approval Prevention",
            "description": "Ensure Pod Leads cannot self-approve their own timesheets, pod changes, or expense requests",
            "role": "pod_lead", "severity": "critical"
        })
    if any(k in low for k in ("hour", "adjustment", "timesheet")):
        items.append({
            "category": "business", "title": "Backdated Hours Adjustment State Modification Lock",
            "description": "Block retrospective modification of locked pay periods without explicit HR Lead approval",
            "role": "lead", "severity": "high"
        })
    if any(k in low for k in ("intern", "role", "rbac")):
        items.append({
            "category": "rbac", "title": "Intern to Admin Vertical Privilege Escalation",
            "description": "Assert Intern/Contractual JWT cannot access HR or Admin mutations",
            "role": "intern", "severity": "critical"
        })


def build_compact_checklist(
    project_name: str,
    endpoints: list[dict],
    models: list[str],
    context_notes: str = ""
) -> list[dict]:
    """Generate comprehensive base checklist without burning tokens unnecessarily."""
    items: list[dict] = []
    # 1. Cyber / Injection & IDOR on discovered endpoints
    for ep in endpoints[:12]:
        path = ep.get("path", "/api")
        method = ep.get("method", "GET").upper()
        name = ep.get("name", "endpoint")
        items.append({
            "category": "cyber",
            "title": f"IDOR & Auth Bypass on {method} {name}",
            "description": f"Test unauthorized resource access and object manipulation on {path}",
            "role": "unauthenticated",
            "severity": "critical" if method in ("POST", "PUT", "DELETE") else "high"
        })
    # 2. RBAC Multi-role Matrix on discovered models
    for mod in models[:8]:
        items.append({
            "category": "rbac",
            "title": f"Multi-role Permission Boundary on {mod}",
            "description": f"Verify Pod Lead / Manager approval and state modification constraints on {mod}",
            "role": "employee",
            "severity": "high"
        })
    # 3. Context & Business Logic Rules
    if context_notes:
        _add_context_rules(context_notes, items)
    # 4. End-User & Code Quality Invariants
    items.append({
        "category": "end-user",
        "title": "Notification Delivery & Event Consistency",
        "description": "Verify that downstream managers receive alerts on state transition approvals",
        "role": "manager",
        "severity": "medium"
    })
    items.append({
        "category": "code",
        "title": "Database Schema & Migration Validation",
        "description": "Ensure zero unhandled exceptions on invalid JSON payloads and constraint violations",
        "role": "system",
        "severity": "high"
    })
    return items
