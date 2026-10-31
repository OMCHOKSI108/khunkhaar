"""Shared fixtures for the P4 regression/knowledge tests.

Every helper writes only inside the isolated KHUNKHAAR root created by the `kh_root` fixture, and
every finding is marked through the same store calls the verifier uses. No helper starts a target.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.findings import models
from core.findings import store as findings_store
from core.metadata import store
from core.project_discovery import detector
from core.regression import process


def register(root: Path) -> str:
    store.init()
    findings_store.init()
    profile = detector.detect(root)
    assert profile.get("valid_project"), profile.get("reason")
    detector.write_profile(profile)
    return profile["project_id"]


def probe_reproduction(kind: str = "validation", *, rule_id: str = "R1",
                       description: str = "Invalid input is rejected",
                       feature: str = "service") -> dict[str, Any]:
    return {
        "type": "probe",
        "probe_kind": kind,
        "invariant": {"rule_id": rule_id, "kind": kind, "description": description,
                      "feature": feature},
    }


def journey_reproduction(feature: str = "service") -> dict[str, Any]:
    return {"type": "journey", "feature": feature, "mutation": {"name": "skip-approval"}}


def add_finding(project_id: str, *, title: str = "Invalid input is accepted",
                status: str = "confirmed", feature: str = "service", category: str = "api",
                severity: str = "medium", source_refs: tuple[str, ...] = ("app/service.py:12",),
                reproduction: dict[str, Any] | None = None, rule_ids: tuple[str, ...] = (),
                expected: str = "invalid input is rejected",
                actual: str = "invalid input was accepted",
                note: str | None = None) -> dict[str, Any]:
    finding = models.Finding(
        project_id=project_id, feature=feature, category=category, title=title,
        description=title, severity=severity, source_refs=list(source_refs),
        rule_ids=list(rule_ids), expected=expected, actual=actual,
        reproduction=reproduction or probe_reproduction(feature=feature))
    row, _ = findings_store.add_finding(finding, actor="test")
    if status != "unverified":
        findings_store.update_status(row["id"], status, actor="verifier",
                                     note=note or f"the verifier marked this finding {status}",
                                     evidence={})
    return findings_store.get_finding(row["id"])  # type: ignore[return-value]


def fake_run(*, violated: bool, rc: int | None = None,
             infrastructure_error: str | None = None,
             duration_ms: int = 5) -> process.PytestRun:
    outcome = {"violated": violated, "expected": "expected", "actual": "observed",
               "infrastructure_error": infrastructure_error}
    return process.PytestRun(rc=(1 if violated else 0) if rc is None else rc,
                             stdout="fixture stdout\n", stderr="", outcome=outcome,
                             timed_out=False, duration_ms=duration_ms)


def write_journey_transcript(kh_root: Path) -> tuple[dict, str]:
    """Create the verification + transcript artifacts that a confirmed journey finding points at."""
    transcript = {
        "journey": {
            "name": "approve an item", "feature": "service",
            "steps": [{"name": "create", "method": "POST", "path": "/items",
                       "payload": {"name": "sample"}, "actor": "user", "expect": "2xx"}],
        },
        "mutation": {
            "name": "skip-approval", "description": "approve without the prerequisite",
            "expectation": "the action is refused", "meaningful": True,
            "steps": [{"name": "approve", "method": "POST", "path": "/items/1/approve",
                       "payload": {}, "actor": "user", "expect": "4xx"}],
        },
    }
    transcript_path = kh_root / "evidence" / "p4" / "transcript.json"
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_path.write_text(json.dumps(transcript), encoding="utf-8")
    verification = {
        "status": "confirmed",
        "attempts": [{"violated": True, "evidence": {
            "transcript": "evidence/p4/transcript.json"}}],
    }
    verification_path = kh_root / "evidence" / "p4" / "verification.json"
    verification_path.write_text(json.dumps(verification), encoding="utf-8")
    return verification, "evidence/p4/verification.json"
