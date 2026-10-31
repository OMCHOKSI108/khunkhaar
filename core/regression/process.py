"""Run one generated test in a separate pytest process and read its structured outcome.

The generated module writes the runtime outcome to a temporary JSON file before asserting, so the
caller can distinguish "the application contradicted the expectation" from "the environment was
unavailable" without parsing pytest prose. Both outputs are kept only in memory here; the caller
redacts them before saving evidence.
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 - a fixed argument vector, no shell
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.shared import paths

REPO_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_LIMIT = 20_000


@dataclass
class PytestRun:
    rc: int
    stdout: str
    stderr: str
    outcome: dict[str, Any] | None
    timed_out: bool
    duration_ms: int

    @property
    def skipped(self) -> bool:
        return "skipped" in self.stdout.lower()

    def to_dict(self) -> dict[str, Any]:
        return {"rc": self.rc, "stdout": self.stdout[-OUTPUT_LIMIT:],
                "stderr": self.stderr[-OUTPUT_LIMIT:], "outcome": self.outcome,
                "timed_out": self.timed_out, "duration_ms": self.duration_ms}


def run_test(test_path: str | Path, *, target: str, users_path: str | None = None,
             db_path: str | None = None, inverted: bool = False,
             timeout: float = 120.0) -> PytestRun:
    path = Path(test_path)
    result_file = paths.tmp_dir() / f"regression-result-{uuid.uuid4().hex}.json"
    env = os.environ.copy()
    env.update({
        "KHUNKHAAR_TARGET": target,
        "KHUNKHAAR_REGRESSION_RESULT": str(result_file),
        "PYTHONPATH": os.pathsep.join(filter(None, [str(REPO_ROOT),
                                                    env.get("PYTHONPATH", "")])),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    if users_path:
        env["KHUNKHAAR_USERS"] = str(users_path)
    if db_path:
        env["KHUNKHAAR_DB"] = str(db_path)
    if inverted:
        env["KHUNKHAAR_REGRESSION_INVERTED"] = "1"
    started = time.monotonic()
    try:
        completed = subprocess.run(  # nosec B603 - no shell, fixed pytest argv
            [sys.executable, "-m", "pytest", str(path), "-q", "-p", "no:cacheprovider",
             "--no-header"],
            cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
            timeout=timeout,
        )
        rc, stdout, stderr, timed_out = completed.returncode, completed.stdout, completed.stderr, False
    except subprocess.TimeoutExpired as exc:
        rc, timed_out = 124, True
        stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
    finally:
        duration_ms = int((time.monotonic() - started) * 1000)
    outcome = _read_result(result_file)
    try:
        result_file.unlink(missing_ok=True)
    except OSError:
        pass
    return PytestRun(rc=rc, stdout=stdout, stderr=stderr, outcome=outcome,
                     timed_out=timed_out, duration_ms=duration_ms)


def _read_result(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None
