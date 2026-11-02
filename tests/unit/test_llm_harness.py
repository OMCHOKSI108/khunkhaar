"""Tests for core.llm.harness — multi-harness LLM module."""
from __future__ import annotations

import json
import os
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from core.llm.harness import (
    CloudflareHarness,
    HarnessError,
    MistralHarness,
    OpenCodeHarness,
    get_harness,
    harness_status,
)


# ── OpenCode ─────────────────────────────────────────────────────────────────

class TestOpenCodeHarness:
    def test_available_when_binary_found(self):
        with patch("shutil.which", return_value="/usr/bin/opencode"):
            assert OpenCodeHarness().available() is True

    def test_not_available_when_binary_missing(self):
        with patch("shutil.which", return_value=None):
            assert OpenCodeHarness().available() is False

    def test_chat_raises_harness_error(self):
        h = OpenCodeHarness()
        with pytest.raises(HarnessError, match="interactive"):
            h.chat("system", "user")


# ── Mistral ──────────────────────────────────────────────────────────────────

class TestMistralHarness:
    def _harness(self, key="test-key"):
        with patch.dict(os.environ, {"MISTRAL_API_KEY": key,
                                     "KHUNKHAAR_MISTRAL_MODEL": "mistral-small-latest"}):
            return MistralHarness()

    def test_available_with_key(self):
        assert self._harness("my-key").available() is True

    def test_not_available_without_key(self):
        with patch.dict(os.environ, {}, clear=True):
            # remove key if present
            env = {k: v for k, v in os.environ.items() if k != "MISTRAL_API_KEY"}
            with patch.dict(os.environ, env, clear=True):
                h = MistralHarness()
                h._api_key = ""
                assert h.available() is False

    def test_chat_success(self):
        h = self._harness()
        mock_resp = json.dumps({
            "choices": [{"message": {"content": "MISTRAL OK"}}]
        }).encode()
        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=MagicMock(read=lambda: mock_resp))
        mock_cm.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_cm):
            result = h.chat("system", "user")
        assert result == "MISTRAL OK"

    def test_chat_rate_limited_raises(self):
        h = self._harness()
        mock_resp = json.dumps({"type": "rate_limited", "message": "Too many requests"}).encode()
        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=MagicMock(read=lambda: mock_resp))
        mock_cm.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_cm):
            with pytest.raises(HarnessError, match="rate limited"):
                h.chat("system", "user")

    def test_chat_http_error_raises(self):
        h = self._harness()
        exc = urllib.error.HTTPError(url="", code=401, msg="Unauthorized", hdrs=None, fp=None)
        exc.read = lambda: b"Unauthorized"
        with patch("urllib.request.urlopen", side_effect=exc):
            with pytest.raises(HarnessError, match="HTTP 401"):
                h.chat("system", "user")

    def test_chat_with_fallback_tries_models(self):
        h = self._harness()
        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                # first 2 models rate limited
                cm = MagicMock()
                cm.__enter__ = MagicMock(return_value=MagicMock(
                    read=lambda: json.dumps({"type": "rate_limited", "message": "rl"}).encode()
                ))
                cm.__exit__ = MagicMock(return_value=False)
                return cm
            else:
                cm = MagicMock()
                cm.__enter__ = MagicMock(return_value=MagicMock(
                    read=lambda: json.dumps({"choices": [{"message": {"content": "OK"}}]}).encode()
                ))
                cm.__exit__ = MagicMock(return_value=False)
                return cm

        with patch("urllib.request.urlopen", side_effect=side_effect):
            result = h.chat_with_fallback("system", "user")
        assert result == "OK"
        assert call_count == 3


# ── Cloudflare ───────────────────────────────────────────────────────────────

class TestCloudflareHarness:
    def _harness(self):
        env = {
            "CF_API_TOKEN": "test-token",
            "CF_ACCOUNT_ID": "test-account",
            "CF_AI_BASE": "https://api.cloudflare.com/client/v4/accounts/test-account/ai/run",
        }
        with patch.dict(os.environ, env):
            return CloudflareHarness()

    def test_available_with_token_and_account(self):
        assert self._harness().available() is True

    def test_not_available_without_token(self):
        with patch.dict(os.environ, {}, clear=True):
            h = CloudflareHarness()
            h._token = ""
            h._account = "something"
            assert h.available() is False

    def test_chat_success_general(self):
        h = self._harness()
        mock_resp = json.dumps({
            "success": True,
            "result": {"response": "CF OK"}
        }).encode()
        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=MagicMock(read=lambda: mock_resp))
        mock_cm.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_cm):
            result = h.chat("system", "user")
        assert result == "CF OK"

    def test_chat_strips_think_tags(self):
        h = self._harness()
        mock_resp = json.dumps({
            "success": True,
            "result": {"response": "<think>some reasoning</think>\nActual answer"}
        }).encode()
        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=MagicMock(read=lambda: mock_resp))
        mock_cm.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_cm):
            result = h.chat("system", "user", task="reasoning")
        assert "<think>" not in result
        assert result == "Actual answer"

    def test_chat_code_uses_code_model(self):
        h = self._harness()
        called_url = []

        def fake_urlopen(req, timeout=None):
            called_url.append(req.full_url)
            mock_resp = json.dumps({"success": True, "result": {"response": "code answer"}}).encode()
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=MagicMock(read=lambda: mock_resp))
            cm.__exit__ = MagicMock(return_value=False)
            return cm

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            h.chat_code("system", "user")
        assert "qwen2.5-coder" in called_url[0]

    def test_chat_reasoning_uses_deepseek(self):
        h = self._harness()
        called_url = []

        def fake_urlopen(req, timeout=None):
            called_url.append(req.full_url)
            mock_resp = json.dumps({"success": True, "result": {"response": "answer"}}).encode()
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=MagicMock(read=lambda: mock_resp))
            cm.__exit__ = MagicMock(return_value=False)
            return cm

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            h.chat_reasoning("system", "user")
        assert "deepseek" in called_url[0]

    def test_chat_http_error_raises(self):
        h = self._harness()
        exc = urllib.error.HTTPError(url="", code=403, msg="Forbidden", hdrs=None, fp=None)
        exc.read = lambda: b"Forbidden"
        with patch("urllib.request.urlopen", side_effect=exc):
            with pytest.raises(HarnessError, match="HTTP 403"):
                h.chat("system", "user")

    def test_chat_api_error_raises(self):
        h = self._harness()
        mock_resp = json.dumps({
            "success": False,
            "errors": [{"message": "Model not found"}]
        }).encode()
        mock_cm = MagicMock()
        mock_cm.__enter__ = MagicMock(return_value=MagicMock(read=lambda: mock_resp))
        mock_cm.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_cm):
            with pytest.raises(HarnessError, match="error"):
                h.chat("system", "user")


# ── get_harness factory ───────────────────────────────────────────────────────

class TestGetHarness:
    def test_auto_returns_opencode_when_binary_found(self):
        with patch("shutil.which", return_value="/usr/bin/opencode"), \
             patch.dict(os.environ, {"KHUNKHAAR_HARNESS": "auto"}):
            h = get_harness()
        assert h.name == "opencode"

    def test_auto_falls_back_to_mistral_when_no_opencode(self):
        with patch("shutil.which", return_value=None), \
             patch.dict(os.environ, {
                 "KHUNKHAAR_HARNESS": "auto",
                 "MISTRAL_API_KEY": "test-key",
             }):
            h = get_harness()
        assert h.name == "mistral"

    def test_auto_falls_back_to_cloudflare_when_no_opencode_no_mistral(self):
        env = {
            "KHUNKHAAR_HARNESS": "auto",
            "CF_API_TOKEN": "cf-token",
            "CF_ACCOUNT_ID": "cf-account",
        }
        with patch("shutil.which", return_value=None), \
             patch.dict(os.environ, env):
            orig = os.environ.pop("MISTRAL_API_KEY", None)
            try:
                h = get_harness()
                assert h.name == "cloudflare"
            finally:
                if orig:
                    os.environ["MISTRAL_API_KEY"] = orig

    def test_explicit_mistral_returns_mistral(self):
        with patch.dict(os.environ, {"MISTRAL_API_KEY": "key"}):
            h = get_harness("mistral")
        assert h.name == "mistral"

    def test_explicit_cloudflare_returns_cloudflare(self):
        with patch.dict(os.environ, {"CF_API_TOKEN": "t", "CF_ACCOUNT_ID": "a"}):
            h = get_harness("cloudflare")
        assert h.name == "cloudflare"

    def test_unknown_harness_raises(self):
        with pytest.raises(HarnessError, match="Unknown harness"):
            get_harness("fakeharness")

    def test_unavailable_explicit_harness_raises(self):
        with patch("shutil.which", return_value=None):
            with pytest.raises(HarnessError, match="not available"):
                get_harness("opencode")


# ── harness_status ────────────────────────────────────────────────────────────

class TestHarnessStatus:
    def test_returns_list_of_three(self):
        statuses = harness_status()
        assert len(statuses) == 3
        names = {s["harness"] for s in statuses}
        assert names == {"opencode", "mistral", "cloudflare"}

    def test_each_has_available_and_note(self):
        for s in harness_status():
            assert "available" in s
            assert "note" in s
            assert isinstance(s["available"], bool)
