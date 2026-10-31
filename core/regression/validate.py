"""Prove a generated test actually observes the recorded defect.

Validation runs the module against the target `validation_runs` times. Every run must observe the
violation; then the same observation is inverted and must pass. Only then does the test move to
`regression/validated/` and become `trusted`. A test that passes while the defect is present, or
that cannot execute, is never promoted: it stays `generated`, becomes `quarantined` when the
configuration asks for that, and the reason is recorded.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from core.metadata import store
from core.regression import process
from core.shared import evidence, paths
from core.shared.config import current

VERDICTS = ("trusted", "not_reproduced", "environment_unavailable", "unreliable")


def _runs_required(override: int | None) -> int:
    value = override if override is not None else int(
        current().get("regression", "validation_runs", 2))
    return max(1, min(value, 3))


def test_path(test: dict[str, Any]) -> Path | None:
    """The validated copy wins once a test is trusted; the generated file is the working copy."""
    return _file(test.get("validated_path")) or _file(test.get("generated_path"))


def _file(raw: Any) -> Path | None:
    if not raw:
        return None
    candidate = Path(str(raw))
    if not candidate.is_absolute():
        candidate = paths.kh_root() / candidate
    return candidate if candidate.is_file() else None


def _classify(run: process.PytestRun) -> str:
    if run.timed_out:
        return "environment_unavailable"
    outcome = run.outcome or {}
    if outcome.get("infrastructure_error"):
        return "environment_unavailable"
    if outcome.get("violated") is True:
        return "reproduced"
    if run.rc == 0:
        return "not_reproduced"
    return "unreliable"


def validate(project_id: str, tests: list[dict[str, Any]], *, target: str,
             users_path: str | None = None, db_path: str | None = None,
             timeout: float = 120.0, runs: int | None = None) -> list[dict[str, Any]]:
    required = _runs_required(runs)
    host = urlsplit(target).hostname or ""
    reports: list[dict[str, Any]] = []
    for test in tests:
        reports.append(_validate_one(project_id, test, target=target, host=host,
                                     users_path=users_path, db_path=db_path,
                                     timeout=timeout, required=required))
    return reports


def _validate_one(project_id: str, test: dict[str, Any], *, target: str, host: str,
                  users_path: str | None, db_path: str | None, timeout: float,
                  required: int) -> dict[str, Any]:
    path = test_path(test)
    if path is None:
        return _finish(project_id, test, "unreliable",
                       "no generated test file exists; regenerate it first", [], None, host)
    normal = [process.run_test(path, target=target, users_path=users_path, db_path=db_path,
                               inverted=False, timeout=timeout)
              for _ in range(required)]
    classes = [_classify(item) for item in normal]
    if "environment_unavailable" in classes:
        return _finish(project_id, test, "environment_unavailable",
                       "the test could not execute against the target; no conclusion about the "
                       "application was drawn", normal, None, host)
    if "unreliable" in classes:
        return _finish(project_id, test, "unreliable",
                       "the test process failed without a runtime outcome", normal, None, host)
    if "not_reproduced" in classes:
        if all(item == "not_reproduced" for item in classes):
            return _finish(project_id, test, "not_reproduced",
                           "the defect was not observed in this environment; the test is not "
                           "trusted", normal, None, host)
        return _finish(project_id, test, "unreliable",
                       "the observation was inconsistent across validation runs", normal, None,
                       host)
    inverted = process.run_test(path, target=target, users_path=users_path, db_path=db_path,
                                inverted=True, timeout=timeout)
    if _classify(inverted) == "environment_unavailable":
        return _finish(project_id, test, "environment_unavailable",
                       "the inverted run could not execute; the test cannot be trusted yet",
                       normal, inverted, host)
    if inverted.rc == 0 and (inverted.outcome or {}).get("violated") is True:
        return _promote(project_id, test, normal, inverted, host)
    return _finish(project_id, test, "unreliable",
                   "the inverted expectation did not pass; the test does not isolate the defect",
                   normal, inverted, host)


def _promote(project_id: str, test: dict[str, Any], normal: list[process.PytestRun],
             inverted: process.PytestRun, host: str) -> dict[str, Any]:
    source_path = _file(test.get("generated_path")) or test_path(test)
    validated_path = None
    if source_path is not None:
        folder = paths.regression_dir("validated", paths.slug_segment(project_id),
                                      str(test["test_id"]))
        copied = evidence.write_bytes(folder, source_path.name, source_path.read_bytes(),
                                      project_id=project_id, kind="regression-test")
        validated_path = evidence.relative(copied)
        intent_file = source_path.parent / "intent.json"
        if intent_file.is_file():
            evidence.write_bytes(folder, "intent.json", intent_file.read_bytes(),
                                 project_id=project_id, kind="regression-intent")
    report = _finish(project_id, test, "trusted",
                     "the defect reproduced in every validation run and the inverted observation "
                     "passed", normal, inverted, host)
    store.update_regression_test(str(test["test_id"]), reliability="trusted",
                                 validated_path=validated_path,
                                 confidence=min(0.99, max(float(test.get("confidence") or 0), 0.9)))
    report["test"] = store.get_regression_test(str(test["test_id"]))
    report["validated_path"] = validated_path
    return report


def _finish(project_id: str, test: dict[str, Any], verdict: str, note: str,
            normal: list[process.PytestRun], inverted: process.PytestRun | None,
            host: str) -> dict[str, Any]:
    folder = paths.regression_dir("validation", paths.slug_segment(project_id))
    payload = {
        "test_id": test["test_id"], "finding_id": test.get("finding_id"),
        "verdict": verdict, "note": note, "target_host": host,
        "normal_runs": [item.to_dict() for item in normal],
        "inverted_run": inverted.to_dict() if inverted else None,
    }
    path = evidence.write_json(folder, f"{test['test_id']}-{evidence.new_run_id('validation')}.json",
                               payload, project_id=project_id, kind="regression-validation")
    updated = test
    if verdict == "unreliable":
        quarantine = bool(current().get("regression", "quarantine_unreliable", True))
        updated = store.update_regression_test(
            str(test["test_id"]), reliability="quarantined" if quarantine else "generated")
    elif verdict == "not_reproduced":
        updated = store.update_regression_test(str(test["test_id"]), reliability="generated")
    return {"test_id": test["test_id"], "verdict": verdict, "note": note,
            "artifact": evidence.relative(path), "test": updated,
            "validated_path": updated.get("validated_path") if isinstance(updated, dict) else None}


def read_intent(test: dict[str, Any]) -> dict[str, Any] | None:
    """The intent stored beside a generated test, for callers that need to display it."""
    path = test_path(test)
    if path is None:
        return None
    intent_file = path.parent / "intent.json"
    try:
        value = json.loads(intent_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None
