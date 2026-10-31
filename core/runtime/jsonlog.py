"""Structured JSONL logging for Phase 2 runs.

One file per run under logs/runs/. Every record is redacted before it is written; the log is the
authoritative timeline used by reports and debugging, not stdout scraping.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from core.shared import paths
from core.shared.redaction import redact_obj


class RunLog:
    def __init__(self, run_id: str):
        self.run_id = run_id
        folder = paths.logs_dir() / "runs"
        folder.mkdir(parents=True, exist_ok=True)
        self.path: Path = folder / f"{run_id}.jsonl"
        self._lock = threading.Lock()
        self.path.touch(mode=0o600, exist_ok=True)
        self.path.chmod(0o600)

    def event(self, kind: str, **fields) -> dict:
        record = {
            "at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "run_id": self.run_id,
            "kind": kind,
            **redact_obj(fields),
        }
        line = json.dumps(record, default=str, ensure_ascii=False)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        return record
