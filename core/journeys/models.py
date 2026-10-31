"""Workflow journeys and their controlled mutations.

A journey is an ordered list of API steps with explicit preconditions. Mutations are generated
from the happy path (skip, repeat, reorder, change actor, stale id, concurrency). Each mutation
carries the outcome that a correct implementation must produce; execution compares that
expectation with what the application actually did.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from core.missions import spec as specmod

ACTIONS = ("submit", "approve", "accept", "complete", "close", "publish")
ACTION_RANK = {action: index for index, action in enumerate(ACTIONS)}
CREATE_FORBIDDEN = ("approve", "reject", "submit", "login", "logout")


@dataclass
class JourneyStep:
    name: str
    method: str
    path: str
    payload: dict[str, Any] | None = None
    actor: str | None = None
    save_as: str | None = None
    expect: str = "2xx"

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "method": self.method, "path": self.path,
                "payload": self.payload, "actor": self.actor, "expect": self.expect}


@dataclass
class Journey:
    name: str
    feature: str
    steps: list[JourneyStep]
    journey_id: str = field(default_factory=lambda: f"J-{uuid.uuid4().hex[:8]}")

    def to_dict(self) -> dict[str, Any]:
        return {"journey_id": self.journey_id, "name": self.name, "feature": self.feature,
                "steps": [s.to_dict() for s in self.steps]}


@dataclass
class Mutation:
    name: str
    description: str
    steps: list[JourneyStep]
    expectation: str
    meaningful: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "expectation": self.expectation, "meaningful": self.meaningful,
                "steps": [s.to_dict() for s in self.steps]}


def build_journey(ops: list[dict[str, Any]], doc: dict[str, Any] | None,
                  feature: str) -> Journey | None:
    create = None
    for op in ops:
        path = op["path"].lower()
        if op["method"] == "POST" and "{" not in path and "login" not in path \
                and not any(word in path for word in CREATE_FORBIDDEN):
            if feature.rstrip("s") in path and op.get("request_schema", {}).get("properties"):
                create = op
                break
    if create is None:
        return None
    action_ops = [op for op in ops if op["method"] == "POST" and "{" in op["path"]
                  and any(action in op["path"].lower() for action in ACTIONS)]
    if not action_ops:
        return None
    steps = [JourneyStep(name="create", method="POST", path=create["path"],
                         payload=specmod.build_payload(doc, create), save_as="id")]
    for op in sorted(action_ops, key=lambda item: ACTION_RANK.get(
            next((word for word in ACTIONS if word in item["path"].lower()), "zzz"), 99)):
        action = next(word for word in ACTIONS if word in op["path"].lower())
        steps.append(JourneyStep(name=action, method="POST",
                                 path=op["path"].replace("{request_id}", "{id}")
                                 .replace("{item_id}", "{id}").replace("{record_id}", "{id}")
                                 .replace("{pk}", "{id}").replace("{uuid}", "{id}"),
                                 payload=specmod.build_payload(doc, op)))
    return Journey(name=f"happy path for {feature}", feature=feature, steps=steps)


def mutate(journey: Journey, roles: list[str]) -> list[Mutation]:
    """Generate a bounded set of mutations with clear, defensible expectations."""
    mutations: list[Mutation] = []
    if len(journey.steps) < 2:
        return mutations
    first, actions = journey.steps[0], journey.steps[1:]

    for index, action in enumerate(actions):
        # skip every prerequisite and jump straight to the action
        skipped = [first, action]
        mutations.append(Mutation(
            name=f"skip to {action.name}",
            description=f"create the resource, then call '{action.name}' without prior steps",
            steps=skipped,
            expectation=f"'{action.name}' is rejected until its documented prerequisites complete",
        ))
        # replay a completed action
        repeat = [*journey.steps[: index + 2], action]
        mutations.append(Mutation(
            name=f"repeat {action.name}",
            description=f"call '{action.name}' a second time after it succeeded",
            steps=repeat,
            expectation=f"the second '{action.name}' is refused or has no additional effect",
            meaningful=action.name in ("approve", "complete", "close"),
        ))
        # stale identifiers
        stale = []
        for step in journey.steps[: index + 2]:
            clone = JourneyStep(**{**step.__dict__})
            clone.path = step.path.replace("{id}", "99999999")
            stale.append(clone)
        mutations.append(Mutation(
            name=f"stale id before {action.name}",
            description="perform the same sequence against a non-existent resource id",
            steps=stale,
            expectation="the non-existent resource is rejected with 404/400",
        ))

    if len(actions) >= 2:
        reordered = [first, *reversed(actions)]
        mutations.append(Mutation(
            name="reorder actions",
            description="execute terminal actions in reverse order",
            steps=reordered,
            expectation="out-of-order transitions are rejected",
        ))

    for role in roles[1:2]:
        changed = []
        for index, step in enumerate(journey.steps):
            clone = JourneyStep(**{**step.__dict__})
            if index > 0:
                clone.actor = role
            changed.append(clone)
        mutations.append(Mutation(
            name=f"change actor to {role}",
            description="start the journey as one user and finish it as another",
            steps=changed,
            expectation="a resource cannot be continued by a user who does not own it",
        ))
    return mutations
