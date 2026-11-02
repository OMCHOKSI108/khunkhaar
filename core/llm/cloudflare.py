"""Cloudflare Workers AI LLM harness (free models)."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from core.llm.base import HarnessError


class CloudflareHarness:
    """REST calls to Cloudflare Workers AI — ALL models are free on the free plan."""

    name = "cloudflare"
    MODELS = {
        "code":      "@cf/qwen/qwen2.5-coder-32b-instruct",
        "reasoning": "@cf/deepseek-ai/deepseek-r1-distill-qwen-32b",
        "general":   "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
        "fast":      "@cf/meta/llama-3.2-3b-instruct",
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
        response = result.get("response", "")
        if "<think>" in response and "</think>" in response:
            end = response.find("</think>")
            response = response[end + len("</think>"):].strip()
        return response

    def chat(self, system: str, user: str, *, max_tokens: int = 4096, task: str = "general") -> str:
        model_map = {
            "code": self._code_model,
            "reasoning": self._reasoning_model,
            "general": self._general_model,
        }
        return self._call(model_map.get(task, self._general_model), system, user, max_tokens)

    def chat_code(self, system: str, user: str, *, max_tokens: int = 4096) -> str:
        return self.chat(system, user, max_tokens=max_tokens, task="code")

    def chat_reasoning(self, system: str, user: str, *, max_tokens: int = 4096) -> str:
        return self.chat(system, user, max_tokens=max_tokens, task="reasoning")
