"""Phase 2 orchestrator.

Owns the run lifecycle: static intelligence -> missions -> specialist agents -> suspected
findings -> independent verification -> persistence and report. Every stage is bounded by the
budget and every failure is recorded instead of being swallowed.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from core.agents.base import ProbeContext, open_db
from core.agents.specialists import AGENT_CLASSES, AgentOutcome
from core.execution.api import ApiClient
from core.findings import models as finding_models
from core.findings import store as findings_store
from core.intelligence import mapper, rules
from core.missions import generator as mission_generator
from core.missions.models import Mission
from core.project_discovery import detector
from core.runtime.budget import Budget, BudgetExceeded
from core.runtime.jsonlog import RunLog
from core.shared import evidence as ev
from core.shared import netguard, paths
from core.shared.config import current
from core.verification.verifier import Verifier

MODE_BUDGETS = {
    "observe": {"steps": 0, "requests": 0, "browser_sessions": 0, "seconds": 120},
    "controlled": {"steps": 80, "requests": 200, "browser_sessions": 4, "seconds": 600},
    "adversarial": {"steps": 160, "requests": 400, "browser_sessions": 8, "seconds": 900},
}

COMMAND_FILTERS = {
    "explore": lambda mission: mission.agent == "user_simulator",
    "security": lambda mission: mission.agent == "security_hunter",
    "api": lambda mission: mission.agent == "api_hunter" or
        (mission.agent == "edge_hunter" and mission.payload.get("probe_kind") == "validation"),
    "workflows": lambda mission: mission.agent in ("workflow_breaker", "db_hunter"),
    "all": lambda mission: True,
}

REMEDIATION = {
    "authorization": "Enforce object-level ownership on every read/write; return 404 for resources "
                     "the caller does not own.",
    "validation": "Validate all request fields at the boundary (non-empty text, positive amounts) "
                  "and reject with 422 before touching persistence.",
    "workflow": "Model the state machine explicitly and reject transitions that are not legal from "
                "the current state.",
    "idempotency": "Accept an idempotency key or deduplicate identical submissions in a single "
                   "transaction.",
    "concurrency": "Take a database-level lock or unique constraint around the check-then-insert.",
    "timezone": "Normalise timestamps to UTC and use the same boundary for writes and reports.",
    "atomicity": "Wrap the operation in one transaction and roll back on failure; never write "
                 "audit rows before the business precondition succeeds.",
    "responsive": "Replace fixed widths with max-width:100%/responsive grid rules; verify at 390px.",
    "ux": "Add accessible names/labels and fix the reported axe-core rules.",
    "auth_boundary": "Require authentication middleware on every private route.",
}

AGENT_SUMMARY_KEYS = ("executed", "failed", "issues", "infrastructure_errors")


@dataclass
class Options:
    root: Path
    target: str | None = None
    db_path: str | None = None
    users_file: str | None = None
    mode: str = "controlled"
    verify: bool = True
    max_steps: int | None = None
    max_requests: int | None = None
    max_seconds: float | None = None
    max_browser_sessions: int | None = None
    max_missions: int = 60
    command: str = "scan"
    authorized: bool = False
    plan_id: str | None = None
    # None means "generate missions"; an empty list is a valid planned set.
    planned_missions: list[dict[str, Any]] | None = None
    planned_regression: dict[str, Any] | None = None
    run_id: str = field(default_factory=lambda: ev.new_run_id("phase2"))


class OrchestratorError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _peak_rss_kb() -> int | None:
    """Process peak RSS from the OS, in KiB. Never estimated from a model."""
    try:
        import resource
        import sys

        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return value // 1024 if sys.platform == "darwin" else value
    except (ImportError, ValueError, OSError):
        return None


_MISSION_FIELDS = frozenset({
    "agent", "title", "kind", "project_id", "description", "category", "severity",
    "invariant_id", "priority", "mode_required", "parallel_safe", "payload", "mission_id",
    "status", "created_at",
})


def mission_from_dict(data: dict[str, Any]) -> Mission:
    return Mission(**{key: value for key, value in data.items() if key in _MISSION_FIELDS})


class Orchestrator:
    def __init__(self, options: Options):
        if options.mode not in MODE_BUDGETS:
            raise OrchestratorError(f"unknown mode '{options.mode}'")
        self.options = options
        self.cfg = current()
        self.log = RunLog(options.run_id)
        defaults = MODE_BUDGETS[options.mode]
        self.budget = Budget(
            max_steps=options.max_steps if options.max_steps is not None else defaults["steps"],
            max_requests=options.max_requests if options.max_requests is not None else defaults["requests"],
            max_browser_sessions=options.max_browser_sessions
            if options.max_browser_sessions is not None else defaults["browser_sessions"],
            max_seconds=options.max_seconds if options.max_seconds is not None else defaults["seconds"],
        )
        self.profile: dict[str, Any] = {}
        self.project_id = ""
        self.app_map: Any = None
        self.invariants: list[dict[str, Any]] = []
        self.spec_doc: dict[str, Any] | None = None
        self.ops: list[dict[str, Any]] = []
        self.users: dict[str, dict[str, Any]] = {}
        self.db = None
        self.summary: dict[str, Any] = {}
        self._metrics: list[dict[str, Any]] = []
        self._metrics_lock = threading.Lock()

    # ---------------------------------------------------------------- discovery
    def discover(self) -> dict[str, Any]:
        root = self.options.root.resolve()
        self.profile = detector.detect(root)
        if not self.profile.get("valid_project"):
            raise OrchestratorError(self.profile.get("reason") or "not a project directory")
        self.project_id = self.profile["project_id"]
        detector.write_profile(self.profile)

        self.app_map = mapper.build(root, self.project_id, rules_classifier=rules.extract_rules)
        self.invariants = rules.invariants_from_rules(self.app_map)

        findings_store.init()
        summary = self.app_map.to_dict()
        map_dir = paths.kh_root() / "intelligence" / self.project_id
        map_dir.mkdir(parents=True, exist_ok=True)
        map_path = map_dir / "app-map.json"
        map_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
        findings_store.save_app_map(self.project_id, ev.relative(map_path), summary)
        for invariant in self.invariants:
            findings_store.add_invariant(self.project_id, invariant)

        discovery = {
            "project_id": self.project_id,
            "root": str(root),
            "frameworks": self.app_map.frameworks,
            "features": self.app_map.features,
            "endpoints": len(self.app_map.endpoints),
            "entities": len(self.app_map.entities),
            "rules": len(self.app_map.rules),
            "invariants": len(self.invariants),
            "files_scanned": self.app_map.files_scanned,
            "truncated": self.app_map.truncated,
            "app_map": ev.relative(map_path),
            "rules_by_kind": _count_by(self.invariants, "kind"),
        }
        self.log.event("discovery", **discovery)
        return discovery

    # ---------------------------------------------------------------- contract
    def connect_target(self) -> dict[str, Any]:
        target = (self.options.target or "").rstrip("/")
        if not target:
            return {"connected": False, "reason": "no --target supplied"}
        parts = urlsplit(target)
        if parts.scheme not in ("http", "https"):
            raise OrchestratorError("target must be an http(s) URL")
        host = netguard.normalize_host(parts.hostname or "")
        port = parts.port
        loopback = host in ("127.0.0.1", "localhost", "::1")
        if not loopback and self.options.mode in ("controlled", "adversarial"):
            approved = [str(entry) for entry in
                        (self.cfg.get("security", "authorized_targets", []) or [])]
            if not any(netguard.host_matches(entry, host, port) for entry in approved):
                raise OrchestratorError(
                    f"target {host}{':' + str(port) if port else ''} is not in "
                    "security.authorized_targets; authorize it before controlled testing")
        if self.options.mode == "adversarial" and not loopback and not self.options.authorized:
            raise OrchestratorError("adversarial mode against a shared host requires --yes")

        client = ApiClient(target, budget=self.budget, log=self.log)
        try:
            self.spec_doc = _fetch_contract(client)
        finally:
            client.close()
        if self.spec_doc:
            from core.missions import spec as specmod
            self.ops = specmod.operations(self.spec_doc)
        self.users = self._load_users()
        if self.options.db_path:
            try:
                self.db = open_db(self.options.db_path)
            except Exception as exc:
                self.log.event("db_unavailable", error=f"{type(exc).__name__}: {exc}")
        connected = {
            "connected": True,
            "target": target,
            "contract": "openapi" if self.spec_doc else "static-map",
            "operations": len(self.ops),
            "users": list(self.users),
            "database": bool(self.db),
        }
        self.log.event("target", **connected)
        return connected

    def _load_users(self) -> dict[str, dict[str, Any]]:
        if not self.options.users_file:
            return {}
        path = Path(self.options.users_file).expanduser()
        if not path.is_file():
            raise OrchestratorError(f"users file not found: {path}")
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise OrchestratorError(f"users file is not valid JSON: {exc}") from exc
        if not isinstance(document, dict):
            raise OrchestratorError("users file must map role -> {username,password}")
        normalized: dict[str, dict[str, Any]] = {}
        for role, value in document.items():
            if isinstance(value, dict):
                normalized[str(role)] = value
            elif isinstance(value, str):
                normalized[str(role)] = {"username": value, "password": ""}
        return normalized

    # ---------------------------------------------------------------- missions
    def plan_missions(self, regression_findings: list[dict[str, Any]]) -> list[Mission]:
        missions = mission_generator.generate(
            self.project_id, self.invariants, self.app_map.features, mode=self.options.mode,
            regression_findings=regression_findings, max_missions=self.options.max_missions,
            ops=self.ops, spec_doc=self.spec_doc)
        selector = COMMAND_FILTERS.get(self.options.command)
        if selector:
            missions = [mission for mission in missions if selector(mission)]
        if self.options.command == "scan":
            return []
        return missions

    # ---------------------------------------------------------------- execution
    async def run(self) -> dict[str, Any]:
        from core.metadata import store as meta_store

        meta_store.init()
        status = "completed"
        try:
            discovery = self.discover()
            # Started after discovery because qa_runs.project_id is a foreign key: the registry
            # row only exists once the profile has been written. Phase 2 passed NULL here, which
            # left per-project execution history unqueryable.
            kind = ("phase3:qa-plan" if self.options.plan_id
                    else f"phase2:{self.options.command}")
            meta_store.start_run(self.options.run_id, self.project_id, kind)
            self.summary = {"run_id": self.options.run_id, "command": self.options.command,
                            "mode": self.options.mode, "plan_id": self.options.plan_id,
                            "discovery": discovery,
                            "target": None, "missions": {}, "agents": {},
                            "budget": {}, "errors": [], "llm_usage": "none"}
            if self.options.command == "scan":
                self.summary["findings"] = findings_store.findings_stats(self.project_id)
                store_summary = self._persist_summary()
                self._finish_run("completed", "static scan")
                self.summary["evidence"] = store_summary
                return self.summary
            if self.options.command == "report":
                from core.reporting.report import build_report
                self.summary["report"] = build_report(self.project_id)
                self._finish_run("completed", "report only")
                return self.summary
            if self.options.command == "verify":
                self.summary["target"] = self.connect_target()
                outcomes = await self.verify_existing()
                self.summary["verification"] = [outcome.to_dict() for outcome in outcomes]
                self.summary["findings"] = findings_store.findings_stats(self.project_id)
                self.summary["evidence"] = self._persist_summary()
                self._finish_run("completed", f"verified {len(outcomes)} finding(s)")
                return self.summary

            self.summary["target"] = self.connect_target()
            if self.options.planned_regression:
                self.summary["regression"] = self._run_planned_regression(
                    self.options.planned_regression)
            prior = findings_store.list_findings(self.project_id)
            if self.options.planned_missions is not None:
                missions = [mission_from_dict(item) for item in self.options.planned_missions]
            else:
                missions = self.plan_missions(prior)
            for mission in missions:
                findings_store.upsert_mission({
                    "id": mission.mission_id, "project_id": self.project_id,
                    "kind": mission.kind, "title": mission.title,
                    "target": self.options.target, "spec": mission.to_dict(),
                    "status": mission.status,
                })
            self.summary["missions"] = {"planned": len(missions), "executed": 0,
                                        "skipped": 0, "failed": 0, "skipped_detail": []}
            outcomes = await self._execute_missions(missions)
            findings_summary = self._record_issues(outcomes)
            verification = []
            if self.options.verify and self.users and self.ops and not self.budget.cancelled:
                verification = await self._verify_new(findings_summary.pop("_new_rows"))
            else:
                findings_summary.pop("_new_rows", None)
            self.summary["findings"] = findings_summary
            self.summary["verification"] = [item.to_dict() for item in verification]
            self.summary["agents"] = _agent_summary(outcomes)
            self.summary["resources"] = self._resources_summary()
            self.summary["budget"] = self.budget.snapshot()
            self.summary["evidence"] = self._persist_summary()
            self.summary["findings_stats"] = findings_store.findings_stats(self.project_id)
            if self.budget.cancelled:
                status = "cancelled"
            note = (f"{self.summary['missions']['executed']} mission(s)"
                    + (", cancelled by the operator" if status == "cancelled" else ""))
            self._finish_run(status, note)
            return self.summary
        except BudgetExceeded as exc:
            status = "cancelled" if getattr(exc, "kind", "") == "cancelled" else "budget_exceeded"
            self.summary.setdefault("errors", []).append(str(exc))
            self.summary["resources"] = self._resources_summary()
            if self.project_id:
                self.summary["evidence"] = self._persist_summary()
            self._finish_run(status, str(exc))
            raise
        except Exception as exc:
            status = "failed"
            self.summary.setdefault("errors", []).append(f"{type(exc).__name__}: {exc}")
            self.summary["resources"] = self._resources_summary()
            if self.project_id:
                self.summary["evidence"] = self._persist_summary()
            self._finish_run(status, str(exc)[:500])
            raise
        finally:
            self.db = None
            self.log.event("run_finished", status=status, budget=self.budget.snapshot())

    def _finish_run(self, status: str, note: str) -> None:
        from core.metadata import store as meta_store

        meta_store.finish_run(self.options.run_id, status, note,
                              peak_rss_kb=self._peak_rss_kb(), llm_usage="none")

    def _peak_rss_kb(self) -> int | None:
        peaks = [metric.get("peak_rss_kb") for metric in self._metrics
                 if metric.get("peak_rss_kb")]
        return max(peaks) if peaks else _peak_rss_kb()

    def _resources_summary(self) -> dict[str, Any]:
        with self._metrics_lock:
            metrics = list(self._metrics)
        per_agent: dict[str, dict[str, Any]] = {}
        for metric in metrics:
            entry = per_agent.setdefault(metric["agent"], {
                "missions": 0, "elapsed_ms": 0, "peak_rss_kb": None})
            entry["missions"] += 1
            entry["elapsed_ms"] += int(metric.get("elapsed_ms") or 0)
            peak = metric.get("peak_rss_kb")
            if peak and (entry["peak_rss_kb"] is None or peak > entry["peak_rss_kb"]):
                entry["peak_rss_kb"] = peak
        return {
            "peak_rss_kb": self._peak_rss_kb(),
            "per_agent": per_agent,
            "llm_usage": "none",
            "note": ("RSS is sampled from the operating system; token and model usage is 'none' "
                     "because no LLM runs in the execution loop"),
        }

    def _run_planned_regression(self, planned: dict[str, Any]) -> dict[str, Any]:
        """Replay exactly the test ids an approved plan named, inside the shared budget."""
        from core.metadata import store as meta_store
        from core.regression import runner

        by_id = {str(test["test_id"]): test for test in meta_store.list_regression_tests(
            self.project_id)}
        selection: dict[str, Any] = {"selected": [], "excluded": []}
        missing: list[dict[str, Any]] = []
        for entry in planned.get("selected_tests") or []:
            test_id = str(entry.get("test_id") or "")
            test = by_id.get(test_id)
            if test is None:
                missing.append({"test_id": test_id,
                                "reason": "the plan named this test but it no longer exists"})
                continue
            selection["selected"].append({
                "test": test,
                "reason": entry.get("reason") or "selected by the approved plan"})
        if not selection["selected"]:
            return {"action_id": planned.get("action_id"), "missing": missing,
                    "executed": False,
                    "note": "no planned regression test file exists; nothing was executed"}
        execution = runner.execute(
            self.project_id, selection, target=self.options.target or "",
            users_path=self.options.users_file, db_path=self.options.db_path,
            budget=self.budget, run_id=self.options.run_id)
        return {"action_id": planned.get("action_id"), "missing": missing, "executed": True,
                "execution": execution}

    def _parallel_workers(self) -> int:
        from core.planning import planner

        return max(1, min(planner.parallel_workers(), self.budget.max_concurrency))

    async def _execute_missions(self, missions: list[Mission]) -> list[AgentOutcome]:
        outcomes: list[AgentOutcome] = []
        if not self.ops and any(mission.agent != "user_simulator" for mission in missions):
            self.summary["missions"]["skipped_detail"].append(
                "no API contract discovered; API missions cannot run")
        index = 0
        while index < len(missions):
            mission = missions[index]
            if not mission.allowed_in(self.options.mode):
                self._skip(mission, f"requires mode '{mission.mode_required}'")
                index += 1
                continue
            if not self.budget.can_continue():
                self._skip_remaining(missions[index:])
                break
            if mission.parallel_safe and self._parallel_workers() > 1:
                batch: list[Mission] = []
                cursor = index
                while cursor < len(missions):
                    candidate = missions[cursor]
                    if not candidate.parallel_safe or not candidate.allowed_in(self.options.mode):
                        break
                    if not self.budget.can_continue():
                        break
                    batch.append(candidate)
                    cursor += 1
                if len(batch) > 1:
                    for candidate, outcome in zip(batch, self._execute_parallel(batch)):
                        outcomes.append(outcome)
                        self._settle_outcome(candidate, outcome)
                    index = cursor
                    continue
            outcome = await self._execute_mission(mission)
            outcomes.append(outcome)
            self._settle_outcome(mission, outcome)
            index += 1
        return outcomes

    def _execute_parallel(self, missions: list[Mission]) -> list[AgentOutcome]:
        """Bounded workers for GET-only missions; each worker owns its own event loop and clients."""
        workers = max(1, min(self._parallel_workers(), len(missions)))
        if workers <= 1:
            return [asyncio.run(self._execute_mission(mission)) for mission in missions]
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="khunkhaar-readonly") as pool:
            futures = [pool.submit(asyncio.run, self._execute_mission(mission))
                       for mission in missions]
            return [future.result() for future in futures]

    def _skip(self, mission: Mission, reason: str) -> None:
        self.summary["missions"]["skipped"] += 1
        self.summary["missions"]["skipped_detail"].append(
            f"{mission.title}: {reason}")

    def _skip_remaining(self, missions: list[Mission]) -> None:
        reason = "cancelled" if self.budget.cancelled else "budget exhausted"
        for mission in missions:
            if mission.allowed_in(self.options.mode):
                self._skip(mission, reason)

    def _settle_outcome(self, mission: Mission, outcome: AgentOutcome) -> None:
        if outcome.infrastructure_errors and not outcome.issues:
            self.summary["missions"]["failed"] += 1
        else:
            self.summary["missions"]["executed"] += 1
        findings_store.upsert_mission({
            "id": mission.mission_id, "project_id": self.project_id,
            "kind": mission.kind, "title": mission.title,
            "target": self.options.target, "spec": mission.to_dict(),
            "status": mission.status,
        })

    def _record_metric(self, mission: Mission, started: float) -> None:
        with self._metrics_lock:
            self._metrics.append({
                "mission_id": mission.mission_id,
                "agent": mission.agent,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "peak_rss_kb": _peak_rss_kb(),
            })

    async def _execute_mission(self, mission: Mission) -> AgentOutcome:
        started = time.monotonic()
        agent = AGENT_CLASSES[mission.agent]()
        ctx = ProbeContext(
            project_id=self.project_id, target=self.options.target or "",
            base_url=(self.options.target or "").rstrip("/"), app_map=self.app_map,
            spec_doc=self.spec_doc, ops=self.ops, users=self.users, db=self.db,
            budget=self.budget, log=self.log, run_id=self.options.run_id,
            mode=self.options.mode,
        )
        try:
            outcome = await agent.execute(ctx, mission)
            mission.status = "completed" if not outcome.infrastructure_errors else "degraded"
            for error in outcome.infrastructure_errors:
                self.log.event("mission_infrastructure_error", mission=mission.mission_id,
                               agent=mission.agent, error=error)
            self.log.event("mission_done", mission=mission.mission_id, agent=mission.agent,
                           issues=len(outcome.issues),
                           infrastructure_errors=len(outcome.infrastructure_errors))
            return outcome
        except BudgetExceeded:
            mission.status = "budget_exceeded"
            raise
        except Exception as exc:
            mission.status = "failed"
            outcome = AgentOutcome(mission.agent, mission)
            outcome.infrastructure_errors.append(f"{type(exc).__name__}: {exc}")
            self.log.event("mission_failed", mission=mission.mission_id,
                           error=f"{type(exc).__name__}: {exc}")
            return outcome
        finally:
            ctx.close()
            self._record_metric(mission, started)

    # ---------------------------------------------------------------- findings
    def _record_issues(self, outcomes: list[AgentOutcome]) -> dict[str, Any]:
        created = updated = 0
        new_rows: list[dict[str, Any]] = []
        for outcome in outcomes:
            for issue in outcome.issues:
                mission = outcome.mission
                invariant = issue.get("invariant") or {}
                result = issue.get("result") or {}
                feature = invariant.get("feature") or mission.payload.get("feature") or "application"
                rule_id = invariant.get("rule_id")
                title = issue.get("title") or invariant.get("description") or mission.title
                evidence = result.get("evidence") or {}
                finding = finding_models.Finding(
                    project_id=self.project_id,
                    feature=str(feature),
                    category=issue.get("category", "functional"),
                    title=title,
                    description=(f"Rule: {invariant.get('description')}. "
                                 f"Expected: {result.get('expected')}. "
                                 f"Observed: {result.get('actual')}."),
                    severity=issue.get("severity", "medium"),
                    role=self.users and next(iter(self.users)) or "anonymous",
                    initial_state=f"status={self.options.mode} target={self.options.target}",
                    steps=result.get("steps") or [],
                    expected=result.get("expected") or "",
                    actual=result.get("actual") or "",
                    evidence=evidence,
                    remediation=REMEDIATION.get(issue.get("probe_kind", ""), ""),
                    source_refs=[invariant["source"]] if invariant.get("source") else [],
                    rule_ids=[rule_id] if rule_id else [],
                    invariant_id=invariant.get("id") or mission.invariant_id or "",
                    reproduction=issue.get("reproduction") or {},
                    status="unverified",
                    confidence=0.4,
                )
                row, was_created = findings_store.add_finding(
                    finding, actor=outcome.agent, note=mission.title)
                if was_created:
                    created += 1
                    new_rows.append(row)
                else:
                    updated += 1
                    new_rows.append(row)
        stats = findings_store.findings_stats(self.project_id)
        return {"created": created, "updated": updated, "by_status": stats["by_status"],
                "total": stats["total"], "_new_rows": new_rows}

    async def _verify_new(self, rows: list[dict[str, Any]]) -> list[Any]:
        verifier = Verifier(
            project_id=self.project_id, target=self.options.target,
            base_url=(self.options.target or "").rstrip("/"), app_map=self.app_map,
            spec_doc=self.spec_doc, ops=self.ops, users=self.users, db=self.db,
            budget=self.budget, log=self.log, run_id=self.options.run_id,
        )
        outcomes = []
        seen: set[str] = set()
        for row in rows:
            if not row or row["id"] in seen:
                continue
            seen.add(row["id"])
            try:
                outcomes.append(await verifier.verify(row))
            except BudgetExceeded:
                raise
            except Exception as exc:
                self.log.event("verification_error", finding=row["id"],
                               error=f"{type(exc).__name__}: {exc}")
        return outcomes

    async def verify_existing(self) -> list[Any]:
        verifier = Verifier(
            project_id=self.project_id, target=self.options.target,
            base_url=(self.options.target or "").rstrip("/"), app_map=self.app_map,
            spec_doc=self.spec_doc, ops=self.ops, users=self.users, db=self.db,
            budget=self.budget, log=self.log, run_id=self.options.run_id,
        )
        rows = [row for row in findings_store.list_findings(self.project_id)
                if row["status"] == "unverified"]
        return [await verifier.verify(row) for row in rows]

    def _persist_summary(self) -> dict[str, Any]:
        folder = paths.reports_dir("phase2", self.project_id)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{self.options.run_id}-run.json"
        target.write_text(json.dumps(self.summary, indent=2, default=str), encoding="utf-8")
        target.chmod(0o600)
        from core.metadata import store as meta_store
        meta_store.add_report_ref(self.project_id, self.options.run_id, ev.relative(target))
        return {"run": ev.relative(target), "log": ev.relative(self.log.path)}


def _fetch_contract(client: ApiClient) -> dict[str, Any] | None:
    for path in ("/openapi.json", "/api/openapi.json", "/swagger.json", "/v3/api-docs"):
        response = client.get(path)
        if response.ok and isinstance(response.body, dict) and "paths" in response.body:
            return response.body
    return None


def _count_by(items: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        value = str(item.get(key) or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return counts


def _agent_summary(outcomes: list[AgentOutcome]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for outcome in outcomes:
        entry = summary.setdefault(outcome.agent, {
            "missions": 0, "issues": 0, "infrastructure_errors": 0, "notes": []})
        entry["missions"] += 1
        entry["issues"] += len(outcome.issues)
        entry["infrastructure_errors"] += len(outcome.infrastructure_errors)
        entry["notes"].extend(outcome.notes[:5])
    return summary


def run_async(options: Options) -> dict[str, Any]:
    return asyncio.run(Orchestrator(options).run())
