"""Sandbox Policy Engine: security validation and access control."""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse
from core.sandbox.models import NetworkPolicy, SandboxJob

BLOCKED_IP_NETWORKS = [
    ipaddress.ip_network("169.254.169.254/32"),  # AWS/GCP/Azure instance metadata
    ipaddress.ip_network("100.100.100.200/32"),  # Alibaba cloud metadata
]
ALLOWED_PROFILES = {"python_qa", "node_qa", "playwright_qa"}
MOCK_API_PORT = 8731


class PolicyViolation(Exception):
    pass


def validate_target_url(url: str, policy: NetworkPolicy) -> str:
    """Validates target URL against network policy and returns normalized target."""
    if policy == NetworkPolicy.NONE:
        raise PolicyViolation("Network access is strictly DENIED under 'none' policy")

    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    # Check blocked metadata endpoints
    try:
        ip = ipaddress.ip_address(hostname)
        for net in BLOCKED_IP_NETWORKS:
            if ip in net:
                raise PolicyViolation(f"Access to cloud metadata endpoint {ip} is strictly forbidden")
    except ValueError:
        pass  # Hostname is not a raw IP

    if policy == NetworkPolicy.MOCK_ONLY:
        is_mock_host = hostname in ("127.0.0.1", "localhost", "host.docker.internal")
        if not (is_mock_host and port == MOCK_API_PORT):
            raise PolicyViolation(
                f"Policy 'mock_only' permits only the mock API at port {MOCK_API_PORT}, got {hostname}:{port}"
            )
    return url


def validate_sandbox_job(job: SandboxJob) -> None:
    if job.execution_profile not in ALLOWED_PROFILES:
        raise PolicyViolation(f"Invalid profile '{job.execution_profile}'. Allowed: {ALLOWED_PROFILES}")

    if not job.script or not job.script.strip():
        raise PolicyViolation("Sandbox script cannot be empty")

    if len(job.script) > 500_000:
        raise PolicyViolation("Script size exceeds 500 KB limit")

    if job.timeout_seconds <= 0 or job.timeout_seconds > 300:
        raise PolicyViolation("Timeout must be between 1 and 300 seconds")

    # Enforce network policy constraints
    if job.network_policy == NetworkPolicy.CONTROLLED_INTERNET:
        raise PolicyViolation("Controlled internet requires administrative elevation (currently disabled)")

    if job.target_url:
        validate_target_url(job.target_url, job.network_policy)
