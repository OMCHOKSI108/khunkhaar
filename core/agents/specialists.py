"""The eight specialist agents.

Each agent declares what it owns, which tools it may use and how much it may spend, then executes
missions through real probes. Agents never write findings as confirmed: they emit *suspected*
issues with reproduction data, and the independent verifier decides the final status.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.agents.base import ProbeContext, ProbeResult
from core.agents.probes import PROBE_BY_KIND
from core.agents.probes import STANDALONE_PROBES as _STANDALONE
from core.journeys import models as journeys
from core.journeys.runner import run_happy_path, run_mutation
from core.missions.models import Mission

AGENT_SPECS: dict[str, dict[str, Any]] = {
    "user_simulator": {
        "description": "Drives the real UI as several user behaviours and records every screen state.",
        "tools": ["browser", "api"],
        "limits": {"max_steps": 40, "max_browser_sessions": 2},
    },
    "workflow_breaker": {
        "description": "Mutates documented journeys: skip, repeat, reorder, change actor, stale ids.",
        "tools": ["api", "browser", "database"],
        "limits": {"max_requests": 80, "max_journey_mutations": 8},
    },
    "edge_hunter": {
        "description": "Probes empty, missing, negative, extreme, unicode and time-boundary inputs.",
        "tools": ["api"],
        "limits": {"max_requests": 40},
    },
    "security_hunter": {
        "description": "Tests cross-user access, IDOR/BOLA and unauthenticated access, then requires evidence.",
        "tools": ["api", "browser", "database"],
        "limits": {"max_requests": 60},
    },
    "ux_inspector": {
        "description": "Checks responsive layout, accessibility and link/console health with browser evidence.",
        "tools": ["browser", "api"],
        "limits": {"max_browser_sessions": 3},
    },
    "api_hunter": {
        "description": "Exercises the API contract: invalid payloads, missing fields, type and boundary errors.",
        "tools": ["api"],
        "limits": {"max_requests": 50},
    },
    "db_hunter": {
        "description": "Verifies persistence, atomicity and concurrency against the actual database.",
        "tools": ["api", "database"],
        "limits": {"max_requests": 60},
    },
    "regression_intel": {
        "description": "Re-runs the reproduction of confirmed findings and related code paths.",
        "tools": ["api", "browser", "database"],
        "limits": {"max_requests": 40},
    },
}


@dataclass
class AgentOutcome:
    agent: str
    mission: Mission
    issues: list[dict[str, Any]] = field(default_factory=list)
    observations: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    infrastructure_errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"agent": self.agent, "mission": self.mission.to_dict(),
                "issues": self.issues, "observations": self.observations,
                "notes": self.notes, "infrastructure_errors": self.infrastructure_errors}


def _issue(probe, result: ProbeResult, invariant: dict[str, Any] | None,
           reproduction: dict[str, Any], title: str | None = None) -> dict[str, Any]:
    return {
        "probe_kind": getattr(probe, "kind", reproduction.get("probe_kind", "probe")),
        "title": title or getattr(probe, "title", "Suspected defect"),
        "category": getattr(probe, "category", "functional"),
        "severity": getattr(probe, "severity", "medium"),
        "result": result.to_dict(),
        "invariant": invariant,
        "reproduction": reproduction,
    }


class BaseAgent:
    name = "base"
    tools: list[str] = []
    mode = "controlled"

    def spec(self) -> dict[str, Any]:
        return {"name": self.name, **AGENT_SPECS.get(self.name, {})}

    async def execute(self, ctx: ProbeContext, mission: Mission) -> AgentOutcome:
        raise NotImplementedError

    async def _run_probe(self, ctx: ProbeContext, mission: Mission,
                         probe, invariant: dict | None) -> AgentOutcome:
        outcome = AgentOutcome(self.name, mission)
        result = await ctx.run_probe(probe)
        if result.infrastructure_error:
            outcome.infrastructure_errors.append(result.infrastructure_error)
        if result.violated:
            outcome.issues.append(_issue(
                probe, result, invariant,
                {"type": "probe", "probe_kind": getattr(probe, "kind"),
                 "invariant": invariant}))
        return outcome


class UserSimulatorAgent(BaseAgent):
    name = "user_simulator"
    tools = ["browser", "api"]

    async def execute(self, ctx: ProbeContext, mission: Mission) -> AgentOutcome:
        from core.execution.browser import BrowserExecutor
        from core.exploration.engine import ExplorationEngine

        outcome = AgentOutcome(self.name, mission)
        executor = BrowserExecutor(budget=ctx.budget, log=ctx.log)
        try:
            cookies = ctx.client(ctx.default_role).cookies
            engine = ExplorationEngine(executor, max_steps=int(mission.payload.get("max_steps", 24)),
                                       on_state=lambda state: outcome.notes.append(
                                           f"{state['step']}: {state['url']} "
                                           f"({state['actionable']} actionable)"),
                                       log=ctx.log)
            exploration = await engine.run(ctx.base_url, cookies=cookies)
            outcome.observations = exploration
            for error in exploration.get("errors", [])[:5]:
                outcome.notes.append(f"console/HTTP error on {error['url']}")
        finally:
            await executor.close()
        return outcome


class WorkflowBreakerAgent(BaseAgent):
    name = "workflow_breaker"
    tools = ["api", "browser", "database"]

    async def execute(self, ctx: ProbeContext, mission: Mission) -> AgentOutcome:
        if mission.kind == "journeys":
            return await self._journeys(ctx, mission)
        invariant = mission.payload.get("invariant") or {}
        kind = mission.payload.get("probe_kind") or invariant.get("kind")
        probe_cls = PROBE_BY_KIND.get(kind)
        if not probe_cls:
            outcome = AgentOutcome(self.name, mission)
            outcome.infrastructure_errors.append(f"no probe for kind '{kind}'")
            return outcome
        return await self._run_probe(ctx, mission, probe_cls(invariant), invariant)

    async def _journeys(self, ctx: ProbeContext, mission: Mission) -> AgentOutcome:
        outcome = AgentOutcome(self.name, mission)
        feature = mission.payload.get("feature") or "application"
        journey = journeys.build_journey(ctx.ops, ctx.spec_doc, feature)
        if journey is None:
            outcome.infrastructure_errors.append(
                f"no journey could be built for feature '{feature}'")
            return outcome
        happy = run_happy_path(ctx, journey)
        outcome.observations["happy_path"] = happy.to_dict()
        outcome.notes.append(f"happy path: final HTTP {happy.actual}")
        if happy.violated:
            outcome.notes.append("happy path itself did not complete; mutations may be unreliable")
        mutations = journeys.mutate(journey, ctx.roles)
        executed = 0
        for mutation in mutations:
            if ctx.budget:
                ctx.budget.check_time()
            result = run_mutation(ctx, journey, mutation)
            outcome.observations.setdefault("mutations", []).append(result.to_dict())
            executed += 1
            if result.violated:
                outcome.issues.append({
                    "probe_kind": "workflow",
                    # One finding per feature: several accepted mutations are manifestations of
                    # the same broken state machine and must update one row, not flood the store.
                    "title": f"Workflow mutations were accepted on '{feature}'",
                    "category": "business_logic",
                    "severity": "high",
                    "result": {"violated": True, "expected": result.expected,
                               "actual": f"mutation '{mutation.name}': {result.actual}",
                               "steps": [e["step"] for e in result.executed],
                               "evidence": result.evidence, "details": {"mutation": mutation.name},
                               "infrastructure_error": None},
                    "invariant": None,
                    "reproduction": {"type": "journey", "feature": feature,
                                     "journey": journey.to_dict(), "mutation": mutation.to_dict()},
                })
            if executed >= 8:
                break
        return outcome


class EdgeCaseHunterAgent(BaseAgent):
    name = "edge_hunter"
    tools = ["api"]

    async def execute(self, ctx: ProbeContext, mission: Mission) -> AgentOutcome:
        invariant = mission.payload.get("invariant") or {}
        kind = mission.payload.get("probe_kind") or invariant.get("kind")
        probe_cls = PROBE_BY_KIND.get(kind)
        if not probe_cls:
            outcome = AgentOutcome(self.name, mission)
            outcome.infrastructure_errors.append(f"no probe for kind '{kind}'")
            return outcome
        outcome = await self._run_probe(ctx, mission, probe_cls(invariant), invariant)
        outcome.notes.append("boundary classes exercised: empty, missing, negative, extreme, unicode")
        return outcome


class SecurityHunterAgent(BaseAgent):
    name = "security_hunter"
    tools = ["api", "browser", "database"]

    async def execute(self, ctx: ProbeContext, mission: Mission) -> AgentOutcome:
        if mission.kind.startswith("standalone:"):
            key = mission.kind.split(":", 1)[1]
            return await self._standalone(ctx, mission, key)
        invariant = mission.payload.get("invariant") or {}
        kind = mission.payload.get("probe_kind") or invariant.get("kind")
        probe_cls = PROBE_BY_KIND.get(kind)
        if not probe_cls:
            outcome = AgentOutcome(self.name, mission)
            outcome.infrastructure_errors.append(f"no probe for kind '{kind}'")
            return outcome
        return await self._run_probe(ctx, mission, probe_cls(invariant), invariant)

    async def _standalone(self, ctx: ProbeContext, mission: Mission, key: str) -> AgentOutcome:
        probe_cls = _STANDALONE.get(key)
        if not probe_cls:
            outcome = AgentOutcome(self.name, mission)
            outcome.infrastructure_errors.append(f"unknown standalone probe '{key}'")
            return outcome
        return await self._run_probe(ctx, mission, probe_cls(), None)


class UxInspectorAgent(BaseAgent):
    name = "ux_inspector"
    tools = ["browser", "api"]

    async def execute(self, ctx: ProbeContext, mission: Mission) -> AgentOutcome:
        if mission.kind.startswith("standalone:"):
            key = mission.kind.split(":", 1)[1]
            probe_cls = _STANDALONE.get(key)
            if not probe_cls:
                outcome = AgentOutcome(self.name, mission)
                outcome.infrastructure_errors.append(f"unknown standalone probe '{key}'")
                return outcome
            return await self._run_probe(ctx, mission, probe_cls(), None)
        invariant = mission.payload.get("invariant") or {}
        kind = mission.payload.get("probe_kind") or invariant.get("kind")
        probe_cls = PROBE_BY_KIND.get(kind)
        if not probe_cls:
            outcome = AgentOutcome(self.name, mission)
            outcome.infrastructure_errors.append(f"no probe for kind '{kind}'")
            return outcome
        return await self._run_probe(ctx, mission, probe_cls(invariant), invariant)


class ApiHunterAgent(BaseAgent):
    name = "api_hunter"
    tools = ["api"]

    async def execute(self, ctx: ProbeContext, mission: Mission) -> AgentOutcome:
        invariant = mission.payload.get("invariant") or {}
        kind = mission.payload.get("probe_kind") or invariant.get("kind")
        probe_cls = PROBE_BY_KIND.get(kind)
        if not probe_cls:
            outcome = AgentOutcome(self.name, mission)
            outcome.infrastructure_errors.append(f"no probe for kind '{kind}'")
            return outcome
        return await self._run_probe(ctx, mission, probe_cls(invariant), invariant)


class DbHunterAgent(BaseAgent):
    name = "db_hunter"
    tools = ["api", "database"]

    async def execute(self, ctx: ProbeContext, mission: Mission) -> AgentOutcome:
        if ctx.db is None:
            outcome = AgentOutcome(self.name, mission)
            outcome.infrastructure_errors.append("no database reader configured; persistence unverified")
            return outcome
        invariant = mission.payload.get("invariant") or {}
        kind = mission.payload.get("probe_kind") or invariant.get("kind")
        probe_cls = PROBE_BY_KIND.get(kind)
        if not probe_cls:
            outcome = AgentOutcome(self.name, mission)
            outcome.infrastructure_errors.append(f"no probe for kind '{kind}'")
            return outcome
        return await self._run_probe(ctx, mission, probe_cls(invariant), invariant)


class RegressionIntelAgent(BaseAgent):
    name = "regression_intel"
    tools = ["api", "browser", "database"]

    async def execute(self, ctx: ProbeContext, mission: Mission) -> AgentOutcome:
        invariant = mission.payload.get("invariant") or {}
        kind = mission.payload.get("probe_kind") or invariant.get("kind")
        probe_cls = PROBE_BY_KIND.get(kind)
        if not probe_cls:
            outcome = AgentOutcome(self.name, mission)
            outcome.infrastructure_errors.append(f"no probe for regression kind '{kind}'")
            return outcome
        outcome = await self._run_probe(ctx, mission, probe_cls(invariant), invariant)
        for issue in outcome.issues:
            issue["reproduction"]["finding_id"] = mission.payload.get("finding_id")
        outcome.notes.append(f"regression replay of {mission.payload.get('finding_id')}")
        return outcome


AGENT_CLASSES = {
    "user_simulator": UserSimulatorAgent,
    "workflow_breaker": WorkflowBreakerAgent,
    "edge_hunter": EdgeCaseHunterAgent,
    "security_hunter": SecurityHunterAgent,
    "ux_inspector": UxInspectorAgent,
    "api_hunter": ApiHunterAgent,
    "db_hunter": DbHunterAgent,
    "regression_intel": RegressionIntelAgent,
}


def agent_catalog() -> list[dict[str, Any]]:
    return [AGENT_CLASSES[name]().spec() for name in AGENT_CLASSES]


def agent_for(name: str) -> BaseAgent:
    return AGENT_CLASSES[name]()
