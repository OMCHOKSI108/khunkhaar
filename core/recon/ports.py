"""Dynamic port and running service discovery."""

from __future__ import annotations

import socket
import urllib.request
import urllib.error
import json
from typing import Any
from core.recon.models import PortInfo

COMMON_DEV_PORTS = [
    3000, 3001, 4000, 4200, 5000, 5001, 5173, 5174,
    8000, 8001, 8080, 8501, 8888, 9000, 9090, 5432,
    3306, 6379, 27017
]


def check_port_open(host: str, port: int, timeout: float = 0.3) -> bool:
    """Check if a TCP port is currently open and accepting connections."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def probe_http_service(host: str, port: int, timeout: float = 1.0) -> dict[str, str] | None:
    """Send an HTTP GET probe to classify web / API services."""
    url = f"http://{host}:{port}/"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "KHUNKHAAR-Recon/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            headers = dict(resp.getheaders())
            body = resp.read(2048).decode("utf-8", errors="ignore")
            server = headers.get("Server", headers.get("server", ""))
            return {"server": server, "body": body[:500], "status": str(resp.status)}
    except urllib.error.HTTPError as exc:
        return {"server": str(exc.headers.get("server", "")), "body": "", "status": str(exc.code)}
    except Exception:
        return None


def probe_graphql_endpoint(host: str, port: int, path: str = "/api/graphql", timeout: float = 1.0) -> bool:
    """Probe for a GraphQL query endpoint."""
    url = f"http://{host}:{port}{path}"
    try:
        payload = json.dumps({"query": "{ __typename }"}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
            return bool(data.get("data", {}).get("__typename"))
    except Exception:
        return False
