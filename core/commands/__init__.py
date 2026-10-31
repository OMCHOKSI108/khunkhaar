"""Subcommand registry for the KHUNKHAAR CLI.

Phase 1 and Phase 2 parse a single leading token, which cannot express `project register` or
`retention plan`. This package resolves a one- or two-token command path to a `module:function`
handler, so the top-level CLI stays a dispatcher and each capability owns its own flags.

A Phase 1/2 command is never routed through here, so existing behaviour is untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, NamedTuple


@dataclass(frozen=True)
class CommandSpec:
    path: tuple[str, ...]
    summary: str
    target: str  # "module:function"

    @property
    def name(self) -> str:
        return " ".join(self.path)


def _spec(path: str, summary: str, target: str) -> CommandSpec:
    return CommandSpec(tuple(path.split()), summary, target)


REGISTRY: tuple[CommandSpec, ...] = (
    _spec("status", "platform status: schema, projects, findings, tool health, disk",
          "core.commands.status:run"),
    _spec("projects", "list registered projects with their QA totals",
          "core.commands.projects:run"),
    _spec("project register", "register a local repository for analysis",
          "core.commands.project:register"),
    _spec("project show", "print a registry entry and its on-disk state",
          "core.commands.project:show"),
    _spec("project authorize", "record explicit permission to test a target",
          "core.commands.project:authorize"),
    _spec("config show", "print the effective configuration (credential values never appear)",
          "core.commands.config:run"),
    _spec("git status", "read-only repository state: branch, base, staged, unstaged, untracked",
          "core.commands.git:status"),
    _spec("git analyze", "read-only change set for --base/--head/--commit/--range/"
                         "--working-tree/--branch", "core.commands.git:analyze"),
    _spec("mr analyze", "merge-request review from local refs (optional read-only GitLab "
                        "metadata)", "core.commands.mr:run"),
    _spec("impact", "change impact graph and approximate risk for the latest git analysis",
          "core.commands.impact:run"),
    _spec("regression list", "list generated and validated regression tests",
          "core.commands.regression:run_list"),
    _spec("regression generate", "generate tests from independently confirmed findings",
          "core.commands.regression:run_generate"),
    _spec("regression validate", "prove a generated test reproduces the recorded defect",
          "core.commands.regression:run_validate"),
    _spec("regression run", "execute selected regression tests within a budget",
          "core.commands.regression:run_run"),
    _spec("knowledge build", "distil project knowledge and the global technique library",
          "core.commands.knowledge:build"),
    _spec("knowledge show", "print the project knowledge document",
          "core.commands.knowledge:show"),
    _spec("knowledge retrieve", "rank this project's history against changed paths",
          "core.commands.knowledge:retrieve"),
    _spec("plan", "build a reviewable QA plan from impact, knowledge and regression",
          "core.commands.plan:build"),
    _spec("plan show", "print a stored plan and verify its digest", "core.commands.plan:show"),
    _spec("plan approve", "record approval of an exact plan digest",
          "core.commands.plan:approve"),
    _spec("plan list", "list stored plans for a project", "core.commands.plan:list_plans"),
    _spec("qa", "execute a reviewed plan exactly, or an impact-only run",
          "core.commands.qa:run"),
    _spec("monitor set", "store a monitoring schedule; nothing is installed on the system",
          "core.commands.monitor:set_schedule"),
    _spec("monitor doctor", "print the exact scheduler lines for a stored schedule",
          "core.commands.monitor:doctor"),
    _spec("monitor run-due", "execute due windows and record missed windows honestly",
          "core.commands.monitor:run_due"),
    _spec("monitor pause", "pause a schedule without creating a missed run",
          "core.commands.monitor:run_pause"),
    _spec("monitor resume", "resume a schedule and recalculate the next window",
          "core.commands.monitor:run_resume"),
    _spec("monitor list", "list stored monitoring schedules",
          "core.commands.monitor:list_schedules"),
    _spec("monitor status", "report whether each next window is scheduled, due or missed",
          "core.commands.monitor:status"),
    _spec("monitor history", "list recorded monitoring runs and missed windows",
          "core.commands.monitor:history"),
    _spec("dashboard start", "serve the private read-only dashboard on loopback",
          "core.commands.dashboard:run_start"),
    _spec("retention index", "rebuild the evidence index from disk",
          "core.commands.retention:index"),
    _spec("retention plan", "show what a retention pass would delete (dry run)",
          "core.commands.retention:plan"),
    _spec("retention apply", "delete eligible evidence (requires --confirm)",
          "core.commands.retention:apply"),
)

_INDEX: dict[tuple[str, ...], CommandSpec] = {spec.path: spec for spec in REGISTRY}


def resolve(argv: list[str]) -> tuple[CommandSpec, list[str]] | None:
    """Longest-match resolution over at most two leading tokens.

    A group with exactly one subcommand also answers to the bare group name with flags, so the
    documented `khunkhaar config` behaves the same as `khunkhaar config show`.
    """
    if not argv:
        return None
    for length in (2, 1):
        spec = _INDEX.get(tuple(argv[:length]))
        if spec:
            return spec, argv[length:]
    group = [spec for spec in REGISTRY if len(spec.path) == 2 and spec.path[0] == argv[0]]
    if len(group) == 1 and (len(argv) == 1 or argv[1].startswith("-")):
        return group[0], argv[1:]
    return None


def known_prefix(head: str) -> list[str]:
    """Command paths starting with `head`, used by the unknown-command message."""
    return [spec.name for spec in REGISTRY if spec.path[0] == head]


class Handler(NamedTuple):
    spec: CommandSpec
    call: Callable[[list[str]], int]


def handler(spec: CommandSpec) -> Handler:
    import importlib

    module_name, _, function = spec.target.partition(":")
    call = getattr(importlib.import_module(module_name), function, None)
    if call is None:  # a registry entry without its function is a bug, never a silent no-op
        raise RuntimeError(f"{spec.target} is not callable")
    return Handler(spec, call)


def help_lines() -> list[str]:
    width = max(len(spec.name) for spec in REGISTRY)
    return [f"  khunkhaar {spec.name:<{width}}  {spec.summary}" for spec in REGISTRY]
