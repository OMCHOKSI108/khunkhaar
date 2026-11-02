"""Adaptive 18-Category Test Strategy and Test Case Generator."""

from __future__ import annotations

import re
from typing import Any
from core.recon.models import ProjectRecon, EndpointDef, ModelDef
from core.strategy.models import TestCase, TestStrategy


CATEGORIES = [
    "1. Business Logic & Domain Workflows",
    "2. RBAC & Multi-Role Authorization",
    "3. Authentication & Session Security",
    "4. State Transitions & Lifecycle Integrity",
    "5. Side Effects, Event Logs & Notifications",
    "6. Data Integrity, Foreign Keys & Cascades",
    "7. Boundary Values & Negative Testing",
    "8. SQL Injection & Injection Vulnerabilities",
    "9. IDOR / Horizontal Privilege Escalation",
    "10. Vertical Privilege Escalation",
    "11. Sensitive Data Exposure / PII Leaks",
    "12. API Error Handling & Schema Strictness",
    "13. Concurrency, Race Conditions & Atomicity",
    "14. File Upload & Binary Processing",
    "15. Third-Party Integration & Webhook Failure",
    "16. Idempotency & Replay Attacks",
    "17. Performance Bottlenecks & N+1 Queries",
    "18. End-to-End User Experience & UI Flows",
]


def generate_sqli_test_cases(recon: ProjectRecon) -> list[TestCase]:
    """Generate SQL Injection test cases targeting endpoints with search or filter parameters."""
    cases: list[TestCase] = []
    idx = 1
    for ep in recon.endpoints:
        desc_lower = ep.description.lower()
        if any(term in desc_lower for term in ("search", "filter", "query", "like", "find")):
            cases.append(TestCase(
                id=f"TC-SQLI-{idx:03d}",
                category="8. SQL Injection & Injection Vulnerabilities",
                title=f"SQLi payload injection on {ep.name}",
                priority="P0_CRITICAL",
                target_type="endpoint",
                target=ep.name,
                description=f"Test {ep.name} with boolean, union, and error-based payloads: zzz%' OR true OR 'a'='a",
                steps=[
                    f"Send benign request with no matches (search='nonexistent_str_9999')",
                    f"Send SQL injection payload: search=\"zzz%' OR true OR 'a'='a\"",
                    f"Send SQL injection payload with false condition: search=\"zzz%' OR false OR 'a'='a\"",
                    "Verify result count in injected request matches false condition or is properly escaped"
                ],
                expected_behavior="Query should return 0 results or throw parameterized validation error; must never return unfiltered database rows",
                assertion_type="response_count_invariant"
            ))
            idx += 1
            if idx > 15:  # Cap per category to maintain focus
                break
    return cases

