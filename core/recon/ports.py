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


def discover_active_services(host: str = "127.0.0.1", candidate_ports: list[int] | None = None) -> list[PortInfo]:
    """Scan and identify all active local services on common development ports."""
    ports_to_check = candidate_ports or COMMON_DEV_PORTS
    active: list[PortInfo] = []

    for port in ports_to_check:
        if not check_port_open(host, port):
            continue

        info = PortInfo(port=port, status="open")
        # Check for GraphQL endpoints
        for gq_path in ("/api/graphql", "/graphql"):
            if probe_graphql_endpoint(host, port, gq_path):
                info.service_type = "graphql"
                info.path = gq_path
                info.banner = "GraphQL API"
                break

        # If not GraphQL, probe HTTP
        if info.service_type == "unknown":
            res = probe_http_service(host, port)
            if res:
                body = res.get("body", "").lower()
                server = res.get("server", "")
                if "streamlit" in body or "_stcore" in body:
                    info.service_type = "streamlit"
                    info.banner = "Streamlit Interactive Application"
                elif "next" in server.lower() or "__next" in body:
                    info.service_type = "nextjs"
                    info.banner = "Next.js Web Application"
                elif "vite" in body or "react" in body:
                    info.service_type = "frontend"
                    info.banner = "Single Page App Frontend"
                else:
                    info.service_type = "http"
                    info.banner = f"HTTP Server ({server or 'status ' + res.get('status', '')})"
            elif port in (5432, 5445):
                info.service_type = "database-postgres"
                info.banner = "PostgreSQL Database"
            elif port in (3306,):
                info.service_type = "database-mysql"
                info.banner = "MySQL Database"
            elif port in (27017,):
                info.service_type = "database-mongodb"
                info.banner = "MongoDB"
            elif port in (6379,):
                info.service_type = "cache-redis"
                info.banner = "Redis"

        active.append(info)

    return active
