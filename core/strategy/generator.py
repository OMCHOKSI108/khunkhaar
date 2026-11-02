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


def generate_idor_test_cases(recon: ProjectRecon) -> list[TestCase]:
    """Generate IDOR and horizontal privilege escalation test cases."""
    cases: list[TestCase] = []
    idx = 1
    for ep in recon.endpoints:
        desc_lower = ep.description.lower()
        if "id" in desc_lower or "where" in desc_lower:
            cases.append(TestCase(
                id=f"TC-IDOR-{idx:03d}",
                category="9. IDOR / Horizontal Privilege Escalation",
                title=f"Cross-user IDOR access verification on {ep.name}",
                priority="P0_CRITICAL" if "auth" in desc_lower or ep.auth_required else "P1_HIGH",
                target_type="endpoint",
                target=ep.name,
                description=f"Verify whether {ep.name} restricts access to records owned by other employees/tenants",
                preconditions=["Authenticate as an ordinary standard user (non-admin, non-owner)"],
                steps=[
                    f"Identify a valid target record ID belonging to another user",
                    f"Send request to {ep.name} with foreign record ID using standard user credentials",
                    "Assert response denies access (403 Forbidden / NOT_AUTHORIZED / null)"
                ],
                expected_behavior="Access denied with 403 / NOT_AUTHORIZED error or null result; must not leak other users' details",
                assertion_type="http_status_or_auth_error"
            ))
            idx += 1
            if idx > 15:
                break
    return cases


def generate_rbac_and_privilege_cases(recon: ProjectRecon) -> list[TestCase]:
    """Generate RBAC and Vertical Privilege Escalation test cases."""
    cases: list[TestCase] = []
    idx = 1
    mutation_eps = [e for e in recon.endpoints if e.kind in ("graphql_mutation", "rest") and e.method in ("POST", "PUT", "DELETE", "PATCH")]

    for ep in mutation_eps:
        name_lower = ep.name.lower()
        is_elevated = any(k in name_lower for k in ("approve", "reject", "delete", "create", "update", "config", "admin", "role", "permission"))
        if is_elevated or ep.auth_required:
            cases.append(TestCase(
                id=f"TC-RBAC-{idx:03d}",
                category="2. RBAC & Multi-Role Authorization",
                title=f"Role boundary enforcement on {ep.name}",
                priority="P0_CRITICAL" if is_elevated else "P1_HIGH",
                target_type="endpoint",
                target=ep.name,
                description=f"Attempt {ep.name} with standard employee/user credentials lacking elevated role",
                preconditions=["Login as standard role (USER / EMPLOYEE)"],
                steps=[
                    f"Attempt invocation of {ep.name} with valid payload but standard user token",
                    "Verify request is blocked with NOT_AUTHORIZED or HTTP 403",
                    "Verify state of the target entity did not mutate in the database"
                ],
                expected_behavior="Operation rejected with authorization error; state unchanged",
                assertion_type="http_status_or_auth_error"
            ))
            idx += 1
            if idx > 15:
                break
    return cases


def generate_business_workflow_cases(recon: ProjectRecon) -> list[TestCase]:
    """Generate business logic, side-effect, and state lifecycle test cases."""
    cases: list[TestCase] = []
    # Check for approval / request domains in endpoints and docs
    has_approval = any("approv" in e.name.lower() or "leave" in e.name.lower() or "wfh" in e.name.lower() for e in recon.endpoints)

    if has_approval:
        cases.append(TestCase(
            id="TC-BIZ-001",
            category="1. Business Logic & Domain Workflows",
            title="Multi-stage approval workflow and role handoff",
            priority="P0_CRITICAL",
            target_type="workflow",
            target="RequestApprovalChain",
            description="End-to-end lifecycle: User creates request -> Pod Lead approves -> Manager receives and approves",
            steps=[
                "User applies for Request/Leave via mutation",
                "Pod Lead reviews and approves request via podApprovalRequest",
                "Manager reviews pending approval and approves via managerApprovalRequest",
                "Verify final status transitions to APPROVED across all stages"
            ],
            expected_behavior="Request cleanly traverses all approval states in sequential order",
            assertion_type="state_and_side_effects"
        ))

        cases.append(TestCase(
            id="TC-SIDE-001",
            category="5. Side Effects, Event Logs & Notifications",
            title="Notification dispatch and audit event logging on approval",
            priority="P1_HIGH",
            target_type="workflow",
            target="ApprovalNotifications",
            description="Verify notifications and event logs are emitted when a request is approved",
            steps=[
                "Pod Lead approves a pending request",
                "Query Manager's pending notifications or notification records",
                "Inspect eventLogsEntries table for audit trail record"
            ],
            expected_behavior="Notification record created for Manager and corresponding audit entry in event logs",
            assertion_type="notification_and_db_audit"
        ))

        cases.append(TestCase(
            id="TC-CONC-001",
            category="13. Concurrency, Race Conditions & Atomicity",
            title="Simultaneous double-submission / race condition check",
            priority="P1_HIGH",
            target_type="workflow",
            target="ConcurrentApplication",
            description="Attempt submitting two duplicate or overlapping requests in concurrent requests",
            steps=[
                "Send two simultaneous requests for the same date window concurrently",
                "Inspect database for duplicate rows or conflicting approvals"
            ],
            expected_behavior="System must reject the conflicting duplicate or handle atomic conflict gracefully",
            assertion_type="atomicity_invariant"
        ))

    return cases

