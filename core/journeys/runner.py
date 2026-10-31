"""Journey execution.

Every step is a real authenticated request. Evidence is the ordered transcript: request, status,
response excerpt. A mutation is violated only when its expectation is unambiguous and the
application's final response contradicts it.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from core.agents.base import ProbeContext, json_body
from core.journeys.models import Journey, JourneyStep, Mutation
from core.missions import spec as specmod


@dataclass
class JourneyOutcome:
    journey: Journey
    mutation: Mutation
    executed: list[dict[str, Any]] = field(default_factory=list)
    violated: bool = False
    expected: str = ""
    actual: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    infrastructure_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"journey": self.journey.to_dict(), "mutation": self.mutation.to_dict(),
                "executed": self.executed, "violated": self.violated,
                "expected": self.expected, "actual": self.actual,
                "evidence": self.evidence, "infrastructure_error": self.infrastructure_error}


def _fill(path: str, values: dict[str, Any]) -> str:
    for name, value in values.items():
        path = path.replace("{" + name + "}", str(value))
    return path


def _run_steps(ctx: ProbeContext, steps: list[JourneyStep]) -> tuple[list[dict], int | None]:
    values: dict[str, Any] = {}
    transcript: list[dict[str, Any]] = []
    final_status: int | None = None
    for step in steps:
        role = step.actor or ctx.default_role
        path = _fill(step.path, values)
        response = ctx.client(role).request(step.method, path,
                                            json_body=copy.deepcopy(step.payload))
        final_status = response.status
        transcript.append({"step": step.name, "actor": role, "method": step.method,
                           "path": path, "status": response.status,
                           "body": str(json_body(response))[:800]})
        if step.save_as and response.ok:
            found = specmod.find_id(json_body(response))
            if found is not None:
                values[step.save_as] = found
        if not response.ok:
            break
    return transcript, final_status


def run_happy_path(ctx: ProbeContext, journey: Journey) -> JourneyOutcome:
    transcript, status = _run_steps(ctx, journey.steps)
    all_ok = all(entry["status"] is not None and 200 <= entry["status"] < 400
                 for entry in transcript) and len(transcript) == len(journey.steps)
    evidence = ctx.save_evidence(f"journey-{journey.journey_id}", {
        "journey": journey.to_dict(), "executed": transcript})
    return JourneyOutcome(
        journey=journey, mutation=Mutation("happy path", "unmutated journey", journey.steps,
                                           "every step succeeds"),
        expected="every step of the documented journey succeeds",
        actual=f"{len(transcript)} step(s) executed, final HTTP {status}",
        executed=transcript, violated=not all_ok, evidence={"transcript": evidence},
    )


def run_mutation(ctx: ProbeContext, journey: Journey, mutation: Mutation) -> JourneyOutcome:
    transcript, status = _run_steps(ctx, mutation.steps)
    last = transcript[-1] if transcript else {}
    succeeded = bool(last) and last["status"] is not None and 200 <= last["status"] < 400
    violated = mutation.meaningful and succeeded
    evidence = ctx.save_evidence(f"mutation-{journey.journey_id}", {
        "journey": journey.to_dict(), "mutation": mutation.to_dict(), "executed": transcript})
    return JourneyOutcome(
        journey=journey, mutation=mutation,
        expected=mutation.expectation,
        actual=f"final step '{last.get('step')}' returned HTTP {last.get('status')}",
        executed=transcript, violated=violated, evidence={"transcript": evidence},
    )
