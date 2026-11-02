"""KHUNKHAAR multi-harness LLM client.

Provides a unified interface to send QA prompts to:
  - OpenCode  (default, interactive terminal agent)
  - Mistral   (mistral-small-latest / ministral-8b — free tier)
  - Cloudflare Workers AI (qwen2.5-coder-32b, deepseek-r1, llama-3.3-70b — all free)

Selection order (KHUNKHAAR_HARNESS=auto):
  1. opencode   — if `opencode` binary found on PATH
  2. mistral    — if MISTRAL_API_KEY is set
  3. cloudflare — if CF_API_TOKEN + CF_ACCOUNT_ID are set

Usage::

    from core.llm.harness import get_harness, HarnessError

    harness = get_harness()
    response = harness.chat(system_prompt, user_prompt)
    print(harness.name, response)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404
import urllib.request
import urllib.error
from pathlib import Path
from typing import Protocol

# ── load .env if present ─────────────────────────────────────────────────────

def _load_dotenv() -> None:
    """Load .env from cwd or KHUNKHAAR_ROOT without requiring python-dotenv."""
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


_load_dotenv()


# ── harness protocol ─────────────────────────────────────────────────────────

class HarnessError(Exception):
    """Raised when a harness cannot complete the request."""


class LLMHarness(Protocol):
    name: str

    def chat(self, system: str, user: str, *, max_tokens: int = 4096) -> str:
        """Send a chat turn and return the assistant's reply as a string."""
        ...

    def available(self) -> bool:
        """Return True if this harness is usable right now."""
        ...


# ── OpenCode harness ─────────────────────────────────────────────────────────

class OpenCodeHarness:
    """Launches opencode with a pre-loaded system prompt injected via stdin."""

    name = "opencode"

    def available(self) -> bool:
        return shutil.which("opencode") is not None

    def chat(self, system: str, user: str, *, max_tokens: int = 4096) -> str:
        """For OpenCode we can't easily drive it programmatically — callers
        should use launch() in cli.py instead.  This returns a placeholder so
        the harness is consistent with the protocol."""
        raise HarnessError(
            "OpenCode harness is interactive; use khunkhar CLI launch instead of .chat()"
        )


# ── Mistral harness ──────────────────────────────────────────────────────────

class MistralHarness:
    """REST calls to api.mistral.ai — uses free tier models."""

    name = "mistral"

    # Free models in order of capability (best first)
    FREE_MODELS = [
        "devstral-small-latest",    # code specialist, most capable
        "mistral-small-latest",     # general purpose
        "ministral-8b-latest",      # fast, 8B
        "ministral-3b-latest",      # lightest, fastest
    ]

    def __init__(self) -> None:
        self._api_key = os.environ.get("MISTRAL_API_KEY", "")
        self._base = os.environ.get("MISTRAL_API_BASE", "https://api.mistral.ai/v1")
        self._model = os.environ.get("KHUNKHAAR_MISTRAL_MODEL", "devstral-small-latest")

    def available(self) -> bool:
        return bool(self._api_key)

    def chat(self, system: str, user: str, *, max_tokens: int = 4096) -> str:
        payload = json.dumps({
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
        }).encode()

        req = urllib.request.Request(
            f"{self._base}/chat/completions",
            data=payload,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            raise HarnessError(f"Mistral HTTP {exc.code}: {body}") from exc

        # Rate limit: try next free model
        if data.get("type") == "rate_limited":
            raise HarnessError(f"Mistral rate limited on {self._model}: {data.get('message')}")

        choices = data.get("choices") or []
        if not choices:
            raise HarnessError(f"Mistral returned no choices: {data}")
        return choices[0]["message"]["content"]

    def chat_with_fallback(self, system: str, user: str, *, max_tokens: int = 4096) -> str:
        """Try each free model in order until one succeeds."""
        original = self._model
        for model in self.FREE_MODELS:
            self._model = model
            try:
                return self.chat(system, user, max_tokens=max_tokens)
            except HarnessError as exc:
                if "rate_limited" in str(exc) or "rate limit" in str(exc).lower():
                    continue
                raise
        self._model = original
        raise HarnessError("All Mistral free models are rate-limited. Wait a minute and retry.")


# ── Cloudflare Workers AI harness ────────────────────────────────────────────

class CloudflareHarness:
    """REST calls to Cloudflare Workers AI — ALL models are free on the free plan."""

    name = "cloudflare"

    # Best free models for QA reasoning tasks
    MODELS = {
        "code":      "@cf/qwen/qwen2.5-coder-32b-instruct",         # code analysis, best
        "reasoning": "@cf/deepseek-ai/deepseek-r1-distill-qwen-32b", # deep logic / RBAC
        "general":   "@cf/meta/llama-3.3-70b-instruct-fp8-fast",    # general QA
        "fast":      "@cf/meta/llama-3.2-3b-instruct",              # lightweight fallback
    }

    def __init__(self) -> None:
        self._token = os.environ.get("CF_API_TOKEN", "")
        self._account = os.environ.get("CF_ACCOUNT_ID", "")
        self._base = os.environ.get(
            "CF_AI_BASE",
            f"https://api.cloudflare.com/client/v4/accounts/{self._account}/ai/run"
        )
        self._code_model = os.environ.get("KHUNKHAAR_CF_CODE_MODEL", self.MODELS["code"])
        self._reasoning_model = os.environ.get("KHUNKHAAR_CF_REASONING_MODEL", self.MODELS["reasoning"])
        self._general_model = os.environ.get("KHUNKHAAR_CF_GENERAL_MODEL", self.MODELS["general"])

    def available(self) -> bool:
        return bool(self._token and self._account)

    def _call(self, model: str, system: str, user: str, max_tokens: int) -> str:
        url = f"{self._base}/{model}"
        payload = json.dumps({
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
        }).encode()

        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            raise HarnessError(f"Cloudflare AI HTTP {exc.code}: {body}") from exc

        if not data.get("success"):
            raise HarnessError(f"Cloudflare AI error: {data.get('errors')}")

        result = data.get("result", {})
        # DeepSeek R1 wraps in <think>…</think> — strip reasoning block for cleaner output
        response = result.get("response", "")
        if "<think>" in response and "</think>" in response:
            end = response.find("</think>")
            response = response[end + len("</think>"):].strip()
        return response

    def chat(self, system: str, user: str, *, max_tokens: int = 4096,
             task: str = "general") -> str:
        """task can be 'code', 'reasoning', or 'general'."""
        model_map = {
            "code":      self._code_model,
            "reasoning": self._reasoning_model,
            "general":   self._general_model,
        }
        model = model_map.get(task, self._general_model)
        return self._call(model, system, user, max_tokens)

    def chat_code(self, system: str, user: str, *, max_tokens: int = 4096) -> str:
        """Use the code-optimized model (Qwen2.5-Coder-32B)."""
        return self.chat(system, user, max_tokens=max_tokens, task="code")

    def chat_reasoning(self, system: str, user: str, *, max_tokens: int = 4096) -> str:
        """Use the deep reasoning model (DeepSeek-R1)."""
        return self.chat(system, user, max_tokens=max_tokens, task="reasoning")


# ── auto harness factory ─────────────────────────────────────────────────────

def get_harness(preferred: str | None = None) -> LLMHarness:
    """Return the best available harness.

    Args:
        preferred: "opencode" | "mistral" | "cloudflare" | "auto" | None
                   If None, reads KHUNKHAAR_HARNESS env var (default "auto").

    Returns:
        An LLMHarness instance.

    Raises:
        HarnessError: if no harness is available.
    """
    mode = preferred or os.environ.get("KHUNKHAAR_HARNESS", "auto")

    harnesses: dict[str, LLMHarness] = {
        "opencode":   OpenCodeHarness(),
        "mistral":    MistralHarness(),
        "cloudflare": CloudflareHarness(),
    }

    if mode != "auto":
        h = harnesses.get(mode)
        if h is None:
            raise HarnessError(f"Unknown harness '{mode}'. Choose: opencode, mistral, cloudflare, auto")
        if not h.available():
            raise HarnessError(
                f"Harness '{mode}' is not available. "
                f"Check your API keys / install opencode."
            )
        return h

    # auto: try in preference order
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
            "note": f"free models: devstral-small, mistral-small, ministral-8b, ministral-3b",
        },
        {
            "harness": "cloudflare",
            "available": CloudflareHarness().available(),
            "note": "free: qwen2.5-coder-32b, deepseek-r1-32b, llama-3.3-70b",
        },
    ]
