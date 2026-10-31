"""Executable probes for the Phase 2 specialist agents.

Every probe derives its endpoints from the runtime OpenAPI contract (or the static map), builds
payloads from the schema, executes real requests and returns evidence. None of them knows the
synthetic application's defect list; they only know generic invariant types.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from core.agents.base import (ProbeContext, ProbeResult, body_contains, db_count,
                              guess_table, json_body, text_keywords)
from core.missions import spec as specmod

AUTH_HINTS = ("login", "logout", "session", "signin", "signup", "register", "token", "auth")


def _is_auth(path: str) -> bool:
    lowered = path.lower()
    return any(hint in lowered for hint in AUTH_HINTS)


def _text(operation: dict) -> str:
    return (operation["path"] + " " + operation.get("summary", "") + " "
            + str(operation.get("operation_id") or "")).lower()


def _score(operation: dict, tokens: tuple[str, ...]) -> int:
    text = _text(operation)
    return sum(1 for token in tokens if token and token.lower() in text)


def _rank(operations: list[dict], tokens: tuple[str, ...], feature: str) -> dict | None:
    if not operations:
        return None
    feature_token = feature.rstrip("s").lower()
    matched = [op for op in operations if feature != "application" and feature_token in op["path"].lower()]
    pool = matched or operations
    pool = sorted(pool, key=lambda op: (-_score(op, tokens), len(op["path"]), op["path"]))
    return pool[0]


def create_op(ctx: ProbeContext, feature: str, keywords: tuple[str, ...] = ()) -> dict | None:
    tokens = tuple(dict.fromkeys([feature.rstrip("s").lower()] + [k.lower() for k in keywords if k]))
    candidates = []
    for op in ctx.ops:
        path = op["path"].lower()
        if op["method"] != "POST" or "{" in path or _is_auth(path):
            continue
        if not (op.get("request_schema", {}).get("properties")):
            continue
        if any(word in path for word in ("approve", "reject", "submit", "clock/out", "clock-out")):
            continue
        candidates.append(op)
    return _rank(candidates, tokens, feature)


def detail_op(ctx: ProbeContext, feature: str, keywords: tuple[str, ...] = ()) -> dict | None:
    tokens = tuple(dict.fromkeys([feature.rstrip("s").lower()] + [k.lower() for k in keywords if k]))
    candidates = [op for op in ctx.ops if op["method"] == "GET" and "{" in op["path"]
                  and not _is_auth(op["path"])]
    return _rank(candidates, tokens, feature)


def list_op(ctx: ProbeContext, feature: str) -> dict | None:
    token = feature.rstrip("s").lower()
    candidates = [op for op in ctx.ops if op["method"] == "GET" and "{" not in op["path"]
                  and not _is_auth(op["path"]) and "report" not in op["path"].lower()
                  and "status" not in op["path"].lower()]
    matched = [op for op in candidates if token in op["path"].lower()]
    if matched:
        return matched[0]
    return candidates[0] if candidates else None


def action_op(ctx: ProbeContext, action: str, feature: str = "") -> dict | None:
    candidates = [op for op in ctx.ops if op["method"] == "POST"
                  and action.lower() in _text(op)]
    with_param = [op for op in candidates if "{" in op["path"]]
    return _rank(with_param or candidates, (action.lower(), feature.rstrip("s").lower()), feature)


def _create_resource(ctx: ProbeContext, role: str, feature: str,
                     overrides: dict | None = None, invalid: bool = False,
                     title: str | None = None) -> tuple[Any, dict, dict, Any]:
    """Create one resource and return (id, payload, response, detail)."""
    operation = create_op(ctx, feature)
    if not operation:
        raise RuntimeError(f"no create endpoint discovered for feature '{feature}'")
    payload_overrides = dict(overrides or {})
    field_names = specmod.request_field_names(operation, ctx.spec_doc)
    title_field = next((name for name in field_names
                        if name in ("title", "name", "label", "subject")), None)
    if title and title_field:
        payload_overrides[title_field] = title
    payload = specmod.build_payload(ctx.spec_doc, operation, invalid=invalid,
                                    overrides=payload_overrides)
    client = ctx.client(role)
    response = client.request("POST", operation["path"], json_body=payload)
    resource_id = specmod.find_id(json_body(response))
    if response.ok and resource_id is None and title_field and title:
        listing = client.get(list_op(ctx, feature)["path"]) if list_op(ctx, feature) else None
        for item in specmod.collection(json_body(listing)) if listing else []:
            if isinstance(item, dict) and item.get(title_field) == title:
                resource_id = specmod.find_id(item)
                break
    return resource_id, payload, response, operation


def _count_records(ctx: ProbeContext, feature: str, title: str, title_field: str = "title",
                   role: str | None = None) -> int:
    operation = list_op(ctx, feature)
    if not operation:
        return 0
    response = ctx.client(role or ctx.roles[0]).get(operation["path"])
    records = specmod.collection(json_body(response))
    return sum(1 for item in records if isinstance(item, dict) and item.get(title_field) == title)


def _table_counts(db) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in db.tables():
        if table.startswith("sqlite_"):
            continue
        value = db_count(db, table)
        if value is not None:
            counts[table] = value
    return counts


def _grown(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {table: after[table] - before.get(table, 0)
            for table in after if after[table] > before.get(table, 0)}


class _RuleProbe:
    category = "functional"
    severity = "medium"
    agent = "api_hunter"
    kind = "probe"

    def __init__(self, invariant: dict[str, Any] | None = None):
        self.invariant = invariant or {}
        self.rule_ids = [self.invariant.get("rule_id")] if self.invariant.get("rule_id") else []
        rule_text = self.invariant.get("description") or ""
        self.title = rule_text or self.__class__.__name__
        self.feature = str(self.invariant.get("feature") or "application")
        self.keywords = tuple(text_keywords(rule_text))

    def describe(self) -> str:
        return self.title


# ---------------------------------------------------------------- security


class AuthorizationProbe(_RuleProbe):
    category = "security"
    severity = "high"
    agent = "security_hunter"
    kind = "authorization"

    async def run(self, ctx: ProbeContext) -> ProbeResult:
        roles = ctx.require_users(2)
        owner_role, other_role = roles[0], roles[1]
        detail = detail_op(ctx, self.feature, self.keywords)
        if not detail:
            return ProbeResult(False, "", "", infrastructure_error="no detail endpoint with a path parameter")
        title = specmod.unique_title("KH-IDOR")
        resource_id, payload, created, create_operation = _create_resource(
            ctx, owner_role, self.feature, title=title)
        steps = [f"{owner_role} creates a resource via POST {create_operation['path']}",
                 f"resource id = {resource_id}"]
        if not created.ok or resource_id is None:
            return ProbeResult(False, "owner can create a resource",
                               f"create returned HTTP {created.status}",
                               infrastructure_error=f"setup failed: create HTTP {created.status}")
        target = specmod.fmt_path(detail["path"], resource_id)
        steps.append(f"{other_role} requests GET {target} without owning the resource")
        probe = ctx.fresh_client(other_role).get(target)
        body = json_body(probe)
        leaked = probe.ok and (body_contains(body, title) or str(body.get("id")) == str(resource_id))
        evidence = ctx.save_evidence(f"idor-{resource_id}", {
            "rule": self.rule_ids, "target": target, "status": probe.status,
            "body_excerpt": str(body)[:1500], "expected": "403/404 for a non-owner",
        })
        return ProbeResult(
            violated=leaked,
            expected="a user other than the owner receives 403/404 for another user's resource",
            actual=f"HTTP {probe.status}" + (" and the owner's data was returned" if leaked else ""),
            steps=steps,
            evidence={"probe": evidence},
            details={"status": probe.status, "resource_id": resource_id},
        )


# ---------------------------------------------------------------- API contract


class ValidationProbe(_RuleProbe):
    category = "api"
    severity = "medium"
    agent = "api_hunter"
    kind = "validation"

    async def run(self, ctx: ProbeContext) -> ProbeResult:
        operation = create_op(ctx, self.feature, self.keywords)
        if not operation:
            return ProbeResult(False, "", "", infrastructure_error="no create endpoint discovered")
        overrides = {}
        fields = specmod.request_field_names(operation, ctx.spec_doc)
        if "title" in fields:
            overrides["title"] = ""
        payload = specmod.build_payload(ctx.spec_doc, operation, invalid=True, overrides=overrides)
        response = ctx.client(ctx.default_role).request("POST", operation["path"], json_body=payload)
        accepted = response.ok
        steps = [f"POST {operation['path']} with empty strings and non-positive numbers",
                 f"response HTTP {response.status}"]
        stored = False
        if accepted:
            resource_id = specmod.find_id(json_body(response))
            detail = detail_op(ctx, self.feature, self.keywords)
            if detail and resource_id is not None:
                fetched = ctx.client(ctx.default_role).get(specmod.fmt_path(detail["path"], resource_id))
                stored = fetched.ok and fetched.status == 200
                steps.append(f"GET {specmod.fmt_path(detail['path'], resource_id)} -> HTTP {fetched.status}")
        evidence = ctx.save_evidence("validation", {
            "rule": self.rule_ids, "payload": payload, "status": response.status,
            "body_excerpt": str(json_body(response))[:1500],
        })
        return ProbeResult(
            violated=accepted,
            expected="invalid values (empty required text, non-positive amount) are rejected with 4xx",
            actual=f"HTTP {response.status}" + (" and the invalid resource is readable" if stored else ""),
            steps=steps,
            evidence={"probe": evidence},
            details={"status": response.status, "accepted": accepted},
        )


# ---------------------------------------------------------------- workflow


class WorkflowProbe(_RuleProbe):
    category = "business_logic"
    severity = "high"
    agent = "workflow_breaker"
    kind = "workflow"

    async def run(self, ctx: ProbeContext) -> ProbeResult:
        action = next((word for word in ("approve", "accept", "complete", "close", "publish")
                       if word in (self.title + " " + self.feature).lower()), None)
        if not action:
            action = "approve"
        operation = action_op(ctx, action, self.feature)
        if not operation:
            return ProbeResult(False, "", "", infrastructure_error=f"no '{action}' action endpoint discovered")
        title = specmod.unique_title("KH-FLOW")
        resource_id, _, created, create_operation = _create_resource(ctx, "user", self.feature, title=title)
        steps = [f"create a fresh resource via POST {create_operation['path']} (no prerequisite action)",
                 f"resource id = {resource_id}"]
        if not created.ok or resource_id is None:
            return ProbeResult(False, "", "", infrastructure_error=f"setup create HTTP {created.status}")
        target = specmod.fmt_path(operation["path"], resource_id)
        payload = specmod.build_payload(ctx.spec_doc, operation)
        response = ctx.client(ctx.default_role).request("POST", target, json_body=payload)
        steps.append(f"POST {target} directly from the initial state")
        violation = response.ok
        evidence = ctx.save_evidence(f"workflow-{resource_id}", {
            "rule": self.rule_ids, "target": target, "status": response.status,
            "body_excerpt": str(json_body(response))[:1500],
        })
        return ProbeResult(
            violated=violation,
            expected=f"'{action}' is refused until the documented prerequisite step is completed",
            actual=f"HTTP {response.status}",
            steps=steps,
            evidence={"probe": evidence},
            details={"status": response.status, "action": action},
        )


class IdempotencyProbe(_RuleProbe):
    category = "reliability"
    severity = "high"
    agent = "workflow_breaker"
    kind = "idempotency"

    async def run(self, ctx: ProbeContext) -> ProbeResult:
        operation = create_op(ctx, self.feature, self.keywords)
        if not operation:
            return ProbeResult(False, "", "", infrastructure_error="no create endpoint discovered")
        fields = specmod.request_field_names(operation, ctx.spec_doc)
        title_field = next((name for name in fields if name in ("title", "name", "label", "subject")),
                          None)
        if not title_field:
            return ProbeResult(False, "", "", infrastructure_error="create schema has no title-like field")
        title = specmod.unique_title("KH-DUP")
        payload = specmod.build_payload(ctx.spec_doc, operation, overrides={title_field: title})
        client = ctx.client(ctx.default_role)
        first = client.request("POST", operation["path"], json_body=payload)
        second = client.request("POST", operation["path"], json_body=payload)
        count = _count_records(ctx, self.feature, title, title_field)
        if count == 0 and first.ok:
            count = 1
        steps = [f"POST {operation['path']} twice with the same payload ({title_field}={title})",
                 f"responses HTTP {first.status} and {second.status}",
                 f"resources with that {title_field}: {count}"]
        evidence = ctx.save_evidence("duplicate-submission", {
            "rule": self.rule_ids, "payload": payload, "statuses": [first.status, second.status],
            "count": count,
        })
        return ProbeResult(
            violated=first.ok and second.ok and count > 1,
            expected="repeating the same submission does not create a second resource",
            actual=f"{count} resources created",
            steps=steps,
            evidence={"probe": evidence},
            details={"count": count, "statuses": [first.status, second.status]},
        )


class ConcurrencyProbe(_RuleProbe):
    category = "concurrency"
    severity = "high"
    agent = "workflow_breaker"
    kind = "concurrency"

    async def run(self, ctx: ProbeContext) -> ProbeResult:
        feature = self.feature if self.feature != "application" else "clock"
        operation = None
        for keyword in ("in", "start", "checkin", "check-in", "open", "out"):
            operation = specmod.find_operation(ctx.ops, "POST", (feature, keyword))
            if operation:
                break
        operation = operation or create_op(ctx, feature)
        if not operation:
            return ProbeResult(False, "", "", infrastructure_error=f"no endpoint discovered for '{feature}'")
        client = ctx.client(ctx.default_role)
        payload = specmod.build_payload(ctx.spec_doc, operation)
        table = guess_table(ctx.app_map, feature) or feature
        # Establish the clean precondition (no open shift) so the run is reproducible even after
        # an earlier probe left rows behind. Draining is bounded and only touches this feature.
        reset = None
        tail = operation["path"].rstrip("/").split("/")[-1].lower()
        if tail in ("in", "start", "checkin", "check-in", "open"):
            reset = (specmod.find_operation(ctx.ops, "POST", (feature, "out"))
                     or specmod.find_operation(ctx.ops, "POST", ("clock", "out")))
        reset_statuses: list[int | None] = []
        if reset:
            for _ in range(5):
                drained = client.request("POST", reset["path"])
                reset_statuses.append(drained.status)
                if not drained.ok:
                    break
        before = db_count(ctx.db, table)
        responses = client.parallel("POST", operation["path"], [payload] * 5, max_workers=5)
        after = db_count(ctx.db, table)
        successes = sum(1 for r in responses if r.ok)
        created = (after - before) if (after is not None and before is not None) else successes
        steps = []
        if reset:
            steps.append(f"drain open state: POST {reset['path']} until refusal -> "
                         f"{[s for s in reset_statuses]}")
        steps.extend([f"fire 5 identical concurrent POST {operation['path']} requests",
                      f"{successes} returned 2xx",
                      f"rows added in '{table}': {created if created is not None else 'unknown'}"])
        evidence = ctx.save_evidence("concurrency", {
            "rule": self.rule_ids, "reset_path": reset and reset["path"],
            "reset_statuses": reset_statuses, "statuses": [r.status for r in responses],
            "rows_before": before, "rows_after": after, "table": table,
        })
        return ProbeResult(
            violated=created is not None and created > 1,
            expected="the documented one-per-user / single-transition invariant holds under concurrency",
            actual=f"{created} rows created by 5 concurrent requests" if created is not None
                   else f"{successes} concurrent requests accepted; database unavailable for verification",
            steps=steps,
            evidence={"probe": evidence},
            details={"successes": successes, "created": created, "table": table},
            infrastructure_error=None if ctx.db is not None else
            "database reader unavailable; concurrency count not verified",
        )


# ---------------------------------------------------------------- edge cases


class TimezoneProbe(_RuleProbe):
    category = "business_logic"
    severity = "medium"
    agent = "edge_hunter"
    kind = "timezone"

    async def run(self, ctx: ProbeContext) -> ProbeResult:
        event_op = specmod.find_operation(ctx.ops, "POST", ("event",)) or create_op(ctx, "event")
        report_op = specmod.find_operation(ctx.ops, "GET", ("report",))
        if not event_op or not report_op:
            return ProbeResult(False, "", "", infrastructure_error="event or report endpoint not discovered")
        fields = specmod.request_field_names(event_op, ctx.spec_doc)
        time_field = next((name for name in fields
                           if name in ("occurred_at", "timestamp", "at", "date", "started_at")), None)
        if not time_field:
            return ProbeResult(False, "", "", infrastructure_error="event schema has no timestamp field")
        day = datetime.now(timezone.utc).date() - timedelta(days=30)
        stamp = f"{day.isoformat()}T23:30:00+00:00"
        label = specmod.unique_title("KH-TZ")
        payload = specmod.build_payload(ctx.spec_doc, event_op,
                                        overrides={time_field: stamp, "label": label})
        created = ctx.client(ctx.default_role).request("POST", event_op["path"], json_body=payload)
        if not created.ok:
            return ProbeResult(False, "", "", infrastructure_error=f"event create HTTP {created.status}")
        next_day = day + timedelta(days=1)
        results = {}
        for target_day in (day, next_day):
            response = ctx.client(ctx.default_role).get(report_op["path"],
                                              params={"date": target_day.isoformat()})
            results[target_day.isoformat()] = {
                "status": response.status, "contains": body_contains(json_body(response), label)}
        correct = results[day.isoformat()]["contains"]
        shifted = results[next_day.isoformat()]["contains"]
        steps = [f"create an event at {stamp} (23:30 UTC)",
                 f"request the report for {day.isoformat()}: contains event = {correct}",
                 f"request the report for {next_day.isoformat()}: contains event = {shifted}"]
        evidence = ctx.save_evidence("timezone-boundary", {
            "rule": self.rule_ids, "timestamp": stamp, "reports": results})
        return ProbeResult(
            violated=shifted and not correct,
            expected="an event at 23:30 UTC belongs to the report for that UTC date",
            actual=("it appears only in the next day's report" if shifted and not correct else
                    f"appears in requested date: {correct}"),
            steps=steps,
            evidence={"probe": evidence},
            details=results,
        )


class AtomicityProbe(_RuleProbe):
    category = "database"
    severity = "high"
    agent = "db_hunter"
    kind = "atomicity"

    async def run(self, ctx: ProbeContext) -> ProbeResult:
        if ctx.db is None:
            return ProbeResult(False, "", "", infrastructure_error="no database reader configured")
        client = ctx.client(ctx.default_role)  # performs login before the baseline snapshot
        text = (self.title + " " + self.feature).lower()
        action = next((word for word in ("approve", "approval", "reject", "submit", "complete",
                                          "close", "cancel") if word in text), None)
        if action == "approval":
            action = "approve"
        operation = action_op(ctx, action, self.feature) if action else None
        if operation:
            target = specmod.fmt_path(operation["path"], 99999999)
            payload = specmod.build_payload(ctx.spec_doc, operation)
            before = _table_counts(ctx.db)
            response = client.request("POST", target, json_body=payload)
            after = _table_counts(ctx.db)
            grown = _grown(before, after)
            steps = [f"baseline rows: {before}",
                     f"POST {target} for a resource that does not exist: HTTP {response.status}",
                     f"rows after the rejected call: {after}"]
            evidence = ctx.save_evidence("atomicity-action", {
                "rule": self.rule_ids, "target": target, "status": response.status,
                "before": before, "after": after, "grown": grown})
            return ProbeResult(
                violated=(not response.ok) and bool(grown),
                expected="a failed operation leaves no persisted rows behind",
                actual=(f"HTTP {response.status}, but rows were added: {grown}"
                        if grown else f"HTTP {response.status}, no rows added"),
                steps=steps,
                evidence={"probe": evidence},
                details={"status": response.status, "grown": grown},
            )

        operation = create_op(ctx, self.feature, self.keywords)
        if not operation:
            return ProbeResult(False, "", "", infrastructure_error="no failure-inducing endpoint discovered")
        table = guess_table(ctx.app_map, self.feature)
        if not table:
            tables = [name for name in ctx.db.tables() if self.feature.rstrip("s") in name.lower()]
            table = tables[0] if tables else None
        if not table:
            tables = [name for name in ctx.db.tables() if not name.startswith("sqlite_")]
            table = tables[0] if tables else None
        if not table:
            return ProbeResult(False, "", "", infrastructure_error="no database table matched the feature")
        before = db_count(ctx.db, table)
        payload = specmod.build_payload(ctx.spec_doc, operation, invalid=True)
        response = client.request("POST", operation["path"], json_body=payload)
        after = db_count(ctx.db, table)
        added = (after - before) if (before is not None and after is not None) else None
        steps = [f"count rows in '{table}': {before}",
                 f"POST {operation['path']} with a payload that must fail validation: HTTP {response.status}",
                 f"count rows in '{table}' again: {after}"]
        evidence = ctx.save_evidence("atomicity", {
            "rule": self.rule_ids, "payload": payload, "status": response.status,
            "rows_before": before, "rows_after": after, "table": table})
        return ProbeResult(
            violated=(not response.ok) and added is not None and added > 0,
            expected="a rejected operation leaves no persisted rows behind",
            actual=f"HTTP {response.status}, {added} row(s) added" if added is not None
                   else f"HTTP {response.status}, database count unavailable",
            steps=steps,
            evidence={"probe": evidence},
            details={"status": response.status, "added": added, "table": table},
        )


# ---------------------------------------------------------------- UX


class ResponsiveProbe(_RuleProbe):
    category = "ux"
    severity = "medium"
    agent = "ux_inspector"
    kind = "responsive"

    async def run(self, ctx: ProbeContext) -> ProbeResult:
        from core.execution.browser import BrowserExecutor

        executor = BrowserExecutor(budget=ctx.budget, log=ctx.log)
        try:
            await executor.open(ctx.base_url, device="iphone13", trace=False)
            cookies = ctx.client(ctx.default_role).cookies
            if cookies:
                await executor.session.context.add_cookies(
                    [{"name": name, "value": value, "url": ctx.base_url}
                     for name, value in cookies.items()])
            observations = []
            links = await executor.session.page.evaluate(
                "() => [...document.querySelectorAll('a[href]')].map(a => a.getAttribute('href'))")
            pages = [ctx.base_url]
            seen = {ctx.base_url.rstrip("/")}
            origin = ctx.base_url.rstrip("/")
            for href in links or []:
                if not href or href.startswith(("#", "mailto:", "javascript:")):
                    continue
                url = href if str(href).startswith("http") else origin + "/" + str(href).lstrip("/")
                key = url.rstrip("/")
                if url.startswith(origin) and key not in seen:
                    pages.append(url)
                    seen.add(key)
                if len(pages) >= 6:
                    break
            for url in pages:
                await executor.session.goto(url)
                report = await executor.responsive_report()
                observations.append(report)
            offenders = [report for report in observations if report["horizontal_overflow"]]
            shot = None
            if offenders:
                shot = await executor.screenshot("responsive-overflow")
            evidence = ctx.save_evidence("responsive", {
                "rule": self.rule_ids, "pages": observations, "screenshot": shot})
            steps = ["open the application with a 390px viewport",
                     f"inspect {len(observations)} page(s) for horizontal overflow"]
            return ProbeResult(
                violated=bool(offenders),
                expected="no page requires horizontal scrolling at 390px width",
                actual=(f"{len(offenders)} page(s) overflow: "
                        + ", ".join(o["url"] for o in offenders)) if offenders else "no overflow observed",
                steps=steps,
                evidence={"probe": evidence, **({"screenshot": shot} if shot else {})},
                details={"offenders": offenders},
            )
        finally:
            await executor.close()


class AccessibilityProbe(_RuleProbe):
    category = "accessibility"
    severity = "low"
    agent = "ux_inspector"
    kind = "accessibility"

    def __init__(self, invariant: dict | None = None):
        super().__init__(invariant)
        self.title = "Page accessibility violations"

    async def run(self, ctx: ProbeContext) -> ProbeResult:
        from core.execution.browser import BrowserExecutor

        executor = BrowserExecutor(budget=ctx.budget, log=ctx.log)
        try:
            await executor.open(ctx.base_url, trace=False)
            cookies = ctx.client(ctx.default_role).cookies
            if cookies:
                await executor.session.context.add_cookies(
                    [{"name": name, "value": value, "url": ctx.base_url}
                     for name, value in cookies.items()])
            await executor.session.goto(ctx.base_url)
            scan = await executor.a11y()
            serious = [v for v in scan.get("violations", [])
                       if v.get("impact") in ("critical", "serious")]
            evidence = ctx.save_evidence("accessibility", {
                "rule": self.rule_ids, "violation_count": scan.get("violation_count"),
                "serious": [v.get("id") for v in serious], "axe": scan.get("evidence")})
            return ProbeResult(
                violated=bool(serious),
                expected="no serious or critical axe-core violations on the landing page",
                actual=f"{len(serious)} serious/critical violations: "
                       + ", ".join(sorted({v.get("id", "?") for v in serious})),
                steps=["open the landing page", "run an axe-core scan"],
                evidence={"probe": evidence, "axe": scan.get("evidence")},
                details={"violations": [v.get("id") for v in scan.get("violations", [])]},
            )
        finally:
            await executor.close()


class LinkHealthProbe(_RuleProbe):
    category = "functional"
    severity = "low"
    agent = "ux_inspector"
    kind = "links"

    def __init__(self, invariant: dict | None = None):
        super().__init__(invariant)
        self.title = "Broken links and console errors"

    async def run(self, ctx: ProbeContext) -> ProbeResult:
        from core.execution.browser import BrowserExecutor

        executor = BrowserExecutor(budget=ctx.budget, log=ctx.log)
        try:
            await executor.open(ctx.base_url, trace=False)
            cookies = ctx.client(ctx.default_role).cookies
            if cookies:
                await executor.session.context.add_cookies(
                    [{"name": name, "value": value, "url": ctx.base_url}
                     for name, value in cookies.items()])
            await executor.session.goto(ctx.base_url)
            observation = await executor.observe()
            origin = ctx.base_url.rstrip("/")
            broken = []
            client = ctx.client(ctx.default_role)
            for element in observation["actionable"]:
                href = element.get("href")
                if not href or href.startswith(("#", "mailto:", "javascript:")):
                    continue
                url = href if str(href).startswith("http") else origin + "/" + str(href).lstrip("/")
                if not url.startswith(origin):
                    continue
                response = client.get(url)
                if response.status and response.status >= 400:
                    broken.append({"url": url, "status": response.status})
                if len(broken) >= 5:
                    break
            errors = observation.get("console_errors", [])
            evidence = ctx.save_evidence("link-health", {
                "broken": broken, "console_errors": errors})
            return ProbeResult(
                violated=bool(broken) or bool(errors),
                expected="navigation links resolve and the page logs no console errors",
                actual=f"{len(broken)} broken link(s), {len(errors)} console error(s)",
                steps=["open the landing page", "request every same-origin navigation link"],
                evidence={"probe": evidence},
                details={"broken": broken, "console_errors": errors},
            )
        finally:
            await executor.close()


class AuthBoundaryProbe(_RuleProbe):
    """Requests private endpoints without any credentials; a 2xx is a real access-control gap."""

    category = "security"
    severity = "high"
    agent = "security_hunter"
    kind = "auth_boundary"

    def __init__(self, invariant: dict | None = None):
        super().__init__(invariant)
        self.title = "Unauthenticated access to private endpoints"

    async def run(self, ctx: ProbeContext) -> ProbeResult:
        from core.execution.api import ApiClient

        candidates = []
        for op in ctx.ops:
            path = op["path"].lower()
            if op["method"] != "GET" or _is_auth(path) or "openapi" in path or "docs" in path:
                continue
            # Only JSON endpoints are treated as private API surface; public HTML pages are
            # meant to be reachable without a session and must not be reported as leaks.
            if not any("json" in ctype for ctype in op.get("response_types", [])):
                continue
            candidates.append(op)
        candidates.sort(key=lambda op: ("{" in op["path"], len(op["path"])))
        if not candidates:
            return ProbeResult(False, "", "", infrastructure_error="no private GET endpoint discovered")
        client = ApiClient(ctx.base_url, budget=ctx.budget, log=ctx.log)
        leaked: list[dict[str, Any]] = []
        steps: list[str] = []
        try:
            for op in candidates[:3]:
                path = specmod.fmt_path(op["path"], 1)
                response = client.get(path)
                steps.append(f"GET {path} without credentials -> HTTP {response.status}")
                if response.ok:
                    leaked.append({"path": path, "status": response.status,
                                   "body": str(json_body(response))[:400]})
        finally:
            client.close()
        evidence = ctx.save_evidence("auth-boundary", {"rule": self.rule_ids, "probes": steps,
                                                       "leaked": leaked})
        return ProbeResult(
            violated=bool(leaked),
            expected="private endpoints answer 401/403 without an authenticated session",
            actual=(f"{len(leaked)} endpoint(s) returned 2xx without credentials" if leaked
                    else "every probed endpoint required authentication"),
            steps=steps,
            evidence={"probe": evidence},
            details={"leaked": leaked},
        )


PROBE_BY_KIND = {
    "authorization": AuthorizationProbe,
    "validation": ValidationProbe,
    "workflow": WorkflowProbe,
    "idempotency": IdempotencyProbe,
    "concurrency": ConcurrencyProbe,
    "timezone": TimezoneProbe,
    "atomicity": AtomicityProbe,
    "responsive": ResponsiveProbe,
    "ux": AccessibilityProbe,
    "accessibility": AccessibilityProbe,
    "links": LinkHealthProbe,
    "auth_boundary": AuthBoundaryProbe,
    "other": None,
}


def probes_for_invariant(invariant: dict[str, Any]) -> list[_RuleProbe]:
    kind = invariant.get("kind") or "other"
    cls = PROBE_BY_KIND.get(kind)
    return [cls(invariant)] if cls else []


STANDALONE_PROBES = {
    "accessibility": AccessibilityProbe,
    "links": LinkHealthProbe,
    "auth_boundary": AuthBoundaryProbe,
}


def standalone_probes() -> list[_RuleProbe]:
    return [AccessibilityProbe(), LinkHealthProbe(), AuthBoundaryProbe()]
