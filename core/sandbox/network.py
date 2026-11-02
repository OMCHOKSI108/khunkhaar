"""Network Isolation: proxy barriers, mock server routing, and DNS validation."""
from __future__ import annotations

import os
from core.sandbox.models import NetworkPolicy
from core.server.path_resolver import resolve_docker_host


def get_sandbox_network_env(policy: NetworkPolicy) -> dict[str, str]:
    """Generates sanitized, restricted environment variables for the execution environment."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "NODE_ENV": "test",
        "PYTHONUNBUFFERED": "1",
        "KHUNKHAAR_SANDBOX": "1",
    }

    if policy == NetworkPolicy.NONE:
        # Network = DENY: inject invalid proxy endpoints to prevent outbound connections
        env["http_proxy"] = "http://0.0.0.0:0"
        env["https_proxy"] = "http://0.0.0.0:0"
        env["all_proxy"] = "socks5://0.0.0.0:0"
        env["NO_PROXY"] = ""

    elif policy == NetworkPolicy.MOCK_ONLY:
        # Route specifically to authorized mock server
        mock_host = resolve_docker_host("127.0.0.1")
        mock_url = f"http://{mock_host}:8731"
        env["KHUNKHAAR_MOCK_API_URL"] = mock_url
        env["MOCK_API_URL"] = mock_url
        env["http_proxy"] = ""
        env["https_proxy"] = ""

    elif policy == NetworkPolicy.ALLOWLISTED:
        env["http_proxy"] = ""
        env["https_proxy"] = ""

    return env
