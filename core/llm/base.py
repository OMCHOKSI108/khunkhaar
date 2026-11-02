"""Base definitions, protocol, and dotenv loader for LLM harnesses."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol


def load_dotenv() -> None:
    """Load .env from cwd, KHUNKHAAR_ROOT, or repo root without requiring python-dotenv."""
    candidates = [
        Path.cwd() / ".env",
        Path(os.environ.get("KHUNKHAAR_ROOT", "")) / ".env" if os.environ.get("KHUNKHAAR_ROOT") else None,
        Path(__file__).resolve().parents[2] / ".env",
    ]
    for path in candidates:
        if path and path.is_file():
            with path.open() as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    key = key.strip()
                    val = val.strip().strip('"').strip("'")
                    os.environ.setdefault(key, val)
            break


load_dotenv()


class HarnessError(Exception):
    """Raised when an LLM harness cannot complete the request."""


class LLMHarness(Protocol):
    name: str

    def chat(self, system: str, user: str, *, max_tokens: int = 4096) -> str:
        """Send a chat turn and return the assistant's reply."""
        ...

    def available(self) -> bool:
        """Return True if this harness is usable right now."""
        ...
