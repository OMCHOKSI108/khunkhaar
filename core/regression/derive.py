"""Turn a confirmed finding's reproduction into a test intent.

The intent is the durable, reviewable description of what the test will do. A journey finding
uses the exact mutation transcript captured during verification, so the generated test replays
those requests instead of rebuilding a journey that may have drifted. A probe finding keeps the
probe kind and invariant; the runtime rebuilds the contract from the target at execution time.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from core.findings import store as findings_store
from core.shared import paths

_USERS_BY_KIND = {"authorization": "two roles (owner and a second user)"}
_DB_BY_KIND = {"atomicity", "concurrency"}


def test_id_for(project_id: str, finding_id: str, reproduction: dict[str, Any]) -> str:
    basis = json.dumps({"project": project_id, "finding": finding_id,
                        "reproduction": reproduction}, sort_keys=True, default=str)
    return "RT-" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:10]


def derive(finding: dict[str, Any], *, project_id: str) -> dict[str, Any]:
    reproduction = dict(finding.get("reproduction") or {})
    method = "journey" if reproduction.get("type") == "journey" else "probe"
    captured = _captured_journey(finding) if method == "journey" else None
    if captured:
        reproduction = captured
    kind = str(reproduction.get("probe_kind")
               or (reproduction.get("invariant") or {}).get("kind") or "")
    finding_id = str(finding.get("id") or finding.get("finding_id") or "")
    intent = {
        "schema": 1,
        "test_id": test_id_for(project_id, finding_id, reproduction),
        "project_id": project_id,
        "finding_id": finding_id,
        "title": finding.get("title") or "regression",
        "feature": finding.get("feature") or "application",
        "category": finding.get("category") or "functional",
        "severity": finding.get("severity") or "medium",
        "method": method,
        "objective": finding.get("title") or "",
        "preconditions": [item for item in [finding.get("initial_state")] if item],
        "role": finding.get("role") or "anonymous",
        "steps": list(finding.get("steps") or []),
        "expected": finding.get("expected") or "",
        "actual_when_confirmed": finding.get("actual") or "",
        "source_refs": list(finding.get("source_refs") or []),
        "reproduction": reproduction,
        "captured": bool(captured),
        "dependencies": {
            "target": "required",
            "users": _USERS_BY_KIND.get(kind),
            "database": kind in _DB_BY_KIND,
        },
    }
    return intent


def _captured_journey(finding: dict[str, Any]) -> dict[str, Any] | None:
    """Read the verified mutation transcript from the finding's confirmation event.

    The event stores a reference to the verification artifact; that artifact lists the attempts,
    and a violating attempt points at the transcript the journey runner saved. Only the last
    confirmed attempt is used, and every path is resolved under the KHUNKHAAR root.
    """
    finding_id = str(finding.get("id") or finding.get("finding_id") or "")
    if not finding_id:
        return None
    events = findings_store.list_events(finding_id)
    event = next((item for item in reversed(events)
                  if item.get("action") == "status:confirmed"), None)
    if event is None:
        return None
    evidence = _json_object(event.get("evidence_json"))
    verification_path = _inside_root(evidence.get("verification"))
    verification = _read_json(verification_path)
    attempts = verification.get("attempts") if isinstance(verification, dict) else None
    for attempt in reversed(attempts or []):
        if not attempt.get("violated"):
            continue
        refs = attempt.get("evidence") or {}
        transcript_path = _inside_root(refs.get("transcript"))
        transcript = _read_json(transcript_path)
        if not isinstance(transcript, dict):
            continue
        journey = transcript.get("journey")
        mutation = transcript.get("mutation")
        if isinstance(journey, dict) and isinstance(mutation, dict):
            return {"type": "journey", "journey": journey, "mutation": mutation}
    return None


def _json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _inside_root(reference: Any) -> Path | None:
    if not reference:
        return None
    candidate = Path(str(reference))
    if not candidate.is_absolute():
        candidate = paths.kh_root() / candidate
    try:
        resolved = candidate.resolve()
        resolved.relative_to(paths.kh_root().resolve())
    except (OSError, ValueError):
        return None
    return resolved


def _read_json(target: Path | None) -> Any:
    if target is None or not target.is_file():
        return None
    try:
        with target.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None
