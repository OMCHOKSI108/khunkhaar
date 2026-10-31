"""Write runnable pytest modules from test intents.

A generated module embeds its intent as a Python literal and calls the shared runtime, so it can
be read, reviewed and executed without KHUNKHAAR guessing anything at run time. The intent is
passed through the platform redactor before it is embedded; a value the redactor removed cannot
reappear in a generated file. Regenerating an unchanged finding produces the same test id and the
same bytes; a changed definition resets reliability to `generated` rather than inheriting trust.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from core.metadata import store
from core.regression import derive
from core.shared import evidence, paths
from core.shared.redaction import redact_obj

_FUNCTION_SAFE = re.compile(r"[^a-z0-9]+")

_TEMPLATE = '''"""Generated regression test {test_id} for finding {finding_id}.

The intent below was derived from a finding the independent verifier confirmed. Regenerate with
`khunkhaar regression generate`; execution history is recorded by KHUNKHAAR, not in this file.
"""

from __future__ import annotations

import json
import os

import pytest

from core.regression import runtime

INTENT = {intent}


def test_{function}():
    inverted = (os.environ.get("KHUNKHAAR_REGRESSION_INVERTED") or "").lower() in (
        "1", "true", "yes")
    outcome = runtime.execute(
        INTENT,
        base_url=os.environ.get("KHUNKHAAR_TARGET", ""),
        users_path=os.environ.get("KHUNKHAAR_USERS") or None,
        db_path=os.environ.get("KHUNKHAAR_DB") or None,
    )
    result_path = os.environ.get("KHUNKHAAR_REGRESSION_RESULT")
    if result_path:
        with open(result_path, "w", encoding="utf-8") as handle:
            json.dump(outcome, handle, indent=2, sort_keys=True)
    if outcome["infrastructure_error"]:
        pytest.skip("environment unavailable: " + outcome["infrastructure_error"])
    if inverted:
        assert outcome["violated"] is True, (
            "the recorded defect did not reproduce when the expectation was inverted: "
            + str(outcome["actual"]))
    else:
        assert outcome["violated"] is False, (
            "regression reproduced: expected " + repr(outcome["expected"])
            + ", observed " + repr(outcome["actual"]))
'''


def _function_name(test_id: str, title: str) -> str:
    slug = _FUNCTION_SAFE.sub("_", (title or "regression").lower()).strip("_")[:40] or "case"
    return f"{test_id.lower().replace('-', '_')}_{slug}"


def source_for(intent: dict[str, Any]) -> str:
    return _TEMPLATE.format(test_id=intent["test_id"], finding_id=intent["finding_id"],
                            intent=repr(intent),
                            function=_function_name(intent["test_id"], intent["title"]))


def generate(finding: dict[str, Any], *, project_id: str) -> dict[str, Any]:
    intent = redact_obj(derive.derive(finding, project_id=project_id))
    test_id = str(intent["test_id"])
    folder = paths.regression_dir("generated", paths.slug_segment(project_id), test_id)
    filename = f"test_{_FUNCTION_SAFE.sub('_', test_id.lower()).strip('_')}.py"
    source = source_for(intent)
    existing = store.get_regression_test(test_id)
    previous = _read_existing(existing, filename)
    changed = previous != source.encode("utf-8")
    test_path = evidence.write_bytes(folder, filename, source.encode("utf-8"),
                                     project_id=project_id, kind="regression-test")
    intent_path = evidence.write_json(folder, "intent.json", intent,
                                      project_id=project_id, kind="regression-intent")
    record = {
        "test_id": test_id,
        "project_id": project_id,
        "finding_id": intent["finding_id"],
        "category": intent["category"],
        "method": intent["method"],
        "objective": intent["objective"] or intent["title"],
        "preconditions": "; ".join(intent.get("preconditions") or []),
        "role": intent.get("role"),
        "steps": intent.get("steps") or [],
        "expected": intent.get("expected"),
        "actual": intent.get("actual_when_confirmed"),
        "dependencies": intent.get("dependencies") or {},
        # Execution history moves confidence; an unchanged regeneration must not reset it.
        "reliability": (existing or {}).get("reliability", "generated") if not changed
                       else "generated",
        "confidence": (float((existing or {}).get("confidence") or 0.0) if not changed
                       else float(finding.get("confidence") or 0.0)),
        "generated_path": evidence.relative(test_path),
        "validated_path": None if changed else (existing or {}).get("validated_path"),
    }
    record = store.upsert_regression_test(record)
    return {"test": record, "intent": intent, "path": evidence.relative(test_path),
            "intent_path": evidence.relative(intent_path), "changed": changed}


def generate_for_findings(project_id: str, findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [generate(finding, project_id=project_id) for finding in findings]


def _read_existing(existing: dict[str, Any] | None, filename: str) -> bytes | None:
    if not existing or not existing.get("generated_path"):
        return None
    path = Path(str(existing["generated_path"]))
    if not path.is_absolute():
        path = paths.kh_root() / path
    if path.name != filename or not path.is_file():
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None
