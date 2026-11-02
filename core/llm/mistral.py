"""Mistral AI LLM harness (free tier models)."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from core.llm.base import HarnessError


class MistralHarness:
    """REST calls to api.mistral.ai — uses free tier models."""

    name = "mistral"
    FREE_MODELS = [
        "devstral-small-latest",
        "mistral-small-latest",
        "ministral-8b-latest",
        "ministral-3b-latest",
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

        if data.get("type") == "rate_limited":
            raise HarnessError(f"Mistral rate limited on {self._model}: {data.get('message')}")

        choices = data.get("choices") or []
        if not choices:
            raise HarnessError(f"Mistral returned no choices: {data}")
        return choices[0]["message"]["content"]

    def chat_with_fallback(self, system: str, user: str, *, max_tokens: int = 4096) -> str:
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
