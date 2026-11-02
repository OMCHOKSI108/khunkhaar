"""KHUNKHAAR multi-harness LLM client and resolver."""
from __future__ import annotations

import os
import shutil

from core.llm.base import HarnessError, LLMHarness, load_dotenv
from core.llm.cloudflare import CloudflareHarness
from core.llm.mistral import MistralHarness

__all__ = [
    "HarnessError", "LLMHarness", "OpenCodeHarness",
    "MistralHarness", "CloudflareHarness", "get_harness", "harness_status"
]

load_dotenv()


class OpenCodeHarness:
    """Launches opencode with pre-loaded prompt."""

    name = "opencode"

    def available(self) -> bool:
        return shutil.which("opencode") is not None

    def chat(self, system: str, user: str, *, max_tokens: int = 4096) -> str:
        raise HarnessError(
            "OpenCode harness is interactive; use khunkhar CLI launch instead of .chat()"
        )


def get_harness(preferred: str | None = None) -> LLMHarness:
    """Return the best available harness."""
    mode = preferred or os.environ.get("KHUNKHAAR_HARNESS", "auto")
    harnesses: dict[str, LLMHarness] = {
        "opencode": OpenCodeHarness(),
        "mistral": MistralHarness(),
        "cloudflare": CloudflareHarness(),
    }

    if mode != "auto":
        h = harnesses.get(mode)
        if h is None:
            raise HarnessError(f"Unknown harness '{mode}'. Choose: opencode, mistral, cloudflare, auto")
        if not h.available():
            raise HarnessError(f"Harness '{mode}' is not available. Check your API keys / install opencode.")
        return h

    for name in ("opencode", "mistral", "cloudflare"):
        h = harnesses[name]
        if h.available():
            return h

    raise HarnessError(
        "No LLM harness available.\n"
        "  • Install opencode on PATH, or\n"
        "  • Set MISTRAL_API_KEY in .env, or\n"
        "  • Set CF_API_TOKEN + CF_ACCOUNT_ID in .env"
    )


def harness_status() -> list[dict]:
    """Return availability status of all harnesses (for --doctor)."""
    return [
        {
            "harness": "opencode",
            "available": OpenCodeHarness().available(),
            "note": "interactive terminal agent",
        },
        {
            "harness": "mistral",
            "available": MistralHarness().available(),
            "note": "free models: devstral-small, mistral-small, ministral-8b, ministral-3b",
        },
        {
            "harness": "cloudflare",
            "available": CloudflareHarness().available(),
            "note": "free: qwen2.5-coder-32b, deepseek-r1-32b, llama-3.3-70b",
        },
    ]
