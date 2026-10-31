"""Independent verification.

The discovering agent can only propose. This module re-executes the recorded reproduction with
fresh clients (new logins, no shared cookies) and classifies the result from observed behaviour:

* 2 of 2 reproductions -> confirmed
* 1 of 2               -> likely
* 0 of 2               -> not reproducible
* infrastructure error -> unverified (never a bug claim)
* no reproduction data -> needs human judgment

The classification is written to the finding store as an event, so the verification history is
permanent and auditable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.agents.base import ProbeContext
from core.agents.probes import PROBE_BY_KIND
from core.findings import store as findings_store
from core.journeys import models as journeys
from core.journeys.runner import run_mutation
from core.shared import evidence as ev


@dataclass
class VerificationOutcome:
    finding_id: str
    status: str
    reproductions: int
    attempts: int
    notes: list[str]
    evidence: str
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"finding_id": self.finding_id, "status": self.status,
                "reproductions": self.reproductions, "attempts": self.attempts,
                "notes": self.notes, "evidence": self.evidence, "details": self.details}


class Verifier:
    def __init__(self, *, project_id: str, target: str | None, base_url: str,
                 app_map, spec_doc, ops, users: dict[str, dict[str, Any]], db=None,
                 budget=None, log=None, run_id: str = "phase2", attempts: int = 2):
        self.project_id = project_id
        self.target = target
        self.base_url = base_url
        self.app_map = app_map
        self.spec_doc = spec_doc
        self.ops = ops
        self.users = users
        self.db = db
        self.budget = budget
        self.log = log
        self.run_id = run_id
        self.attempts = max(1, attempts)

    def _context(self) -> ProbeContext:
        return ProbeContext(
            project_id=self.project_id, target=self.target or self.base_url,
            base_url=self.base_url, app_map=self.app_map, spec_doc=self.spec_doc,
            ops=self.ops, users=self.users, db=self.db, budget=self.budget,
            log=self.log, run_id=self.run_id, mode="controlled",
        )

    async def _attempt(self, reproduction: dict[str, Any]) -> dict[str, Any]:
        ctx = self._context()
        try:
            if reproduction.get("type") == "journey":
                feature = reproduction.get("feature") or "application"
                journey = journeys.build_journey(self.ops, self.spec_doc, feature)
                if journey is None:
                    return {"violated": False, "infrastructure_error": "journey could not be rebuilt"}
                name = (reproduction.get("mutation") or {}).get("name")
                mutation = next((m for m in journeys.mutate(journey, ctx.roles)
                                 if m.name == name), None)
                if mutation is None:
                    return {"violated": False,
                            "infrastructure_error": f"mutation '{name}' is no longer generated"}
                result = run_mutation(ctx, journey, mutation)
                return {"violated": result.violated, "expected": result.expected,
                        "actual": result.actual, "evidence": result.evidence,
                        "infrastructure_error": result.infrastructure_error}
            invariant = reproduction.get("invariant") or {}
            kind = reproduction.get("probe_kind") or invariant.get("kind")
            probe_cls = PROBE_BY_KIND.get(kind)
            if probe_cls is None:
                return {"violated": False,
                        "infrastructure_error": f"no probe available for kind '{kind}'"}
            result = await ctx.run_probe(probe_cls(invariant))
            return {"violated": result.violated, "expected": result.expected,
                    "actual": result.actual, "evidence": result.evidence,
                    "infrastructure_error": result.infrastructure_error}
        finally:
            ctx.close()

    async def verify(self, finding: dict[str, Any], actor: str = "independent_verifier") -> VerificationOutcome:
        finding_id = finding["id"]
        reproduction = finding.get("reproduction") or {}
        if not reproduction:
            status = "needs_human"
            note = "no machine-reproducible scenario was recorded; a human must decide"
            evidence_path = self._save(finding_id, {"reproduction": None, "status": status})
            findings_store.update_status(finding_id, status, actor, note,
                                         evidence={"verification": evidence_path})
            return VerificationOutcome(finding_id, status, 0, 0, [note], evidence_path, {})

        attempts: list[dict[str, Any]] = []
        for index in range(self.attempts):
            if self.budget:
                try:
                    self.budget.check_time()
                except Exception as exc:
                    attempts.append({"attempt": index + 1, "violated": False,
                                     "infrastructure_error": f"{type(exc).__name__}: {exc}"})
                    break
            outcome = await self._attempt(reproduction)
            outcome["attempt"] = index + 1
            attempts.append(outcome)

        reproductions = sum(1 for item in attempts if item.get("violated"))
        infrastructure = [item["infrastructure_error"] for item in attempts
                          if item.get("infrastructure_error")]
        usable = len(attempts) - len(infrastructure)
        if usable == 0:
            status = "unverified"
            note = f"verification could not execute: {infrastructure[0]}" if infrastructure else \
                "verification produced no usable attempt"
        elif reproductions >= 2:
            status = "confirmed"
            note = f"reproduced {reproductions}/{len(attempts)} independent attempts"
        elif reproductions == 1:
            status = "likely"
            note = f"reproduced 1/{len(attempts)} independent attempts"
        else:
            status = "not_reproducible"
            note = f"reproduced 0/{len(attempts)} independent attempts"

        evidence_path = self._save(finding_id, {"attempts": attempts, "status": status,
                                                "note": note})
        findings_store.update_status(
            finding_id, status, actor, note,
            evidence={"verification": evidence_path},
            reproducibility=f"{reproductions}/{len(attempts)}",
        )
        if self.log:
            self.log.event("verification", finding=finding_id, status=status,
                           reproductions=reproductions, attempts=len(attempts))
        return VerificationOutcome(finding_id, status, reproductions, len(attempts),
                                   [note, *[f"attempt {i + 1}: {a.get('actual', 'no result')}"
                                            for i, a in enumerate(attempts)]],
                                   evidence_path, {"attempts": attempts})

    def _save(self, finding_id: str, payload: dict[str, Any]) -> str:
        folder = ev.evidence_root("phase2", self.run_id) / "verification"
        path = ev.write_json(folder, f"{ev.slugify(finding_id, 60)}.json", payload,
                             project_id=self.project_id, run_id=self.run_id, kind="verification")
        return ev.relative(path)

    async def verify_many(self, findings: list[dict[str, Any]]) -> list[VerificationOutcome]:
        outcomes = []
        for finding in findings:
            outcomes.append(await self.verify(finding))
        return outcomes
