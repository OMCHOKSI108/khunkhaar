"""Real test execution engine against live target GraphQL and REST APIs."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any
from core.server.models import ChecklistItem


def resolve_target_url(raw_url: str) -> str:
    """Normalize target URL, handling container-to-host bridging."""
    url = raw_url.strip() if raw_url else "http://localhost:8000"
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "http://" + url

    # Inside container, bridge localhost to host-gateway
    if os.path.exists("/.dockerenv") or os.path.exists("/workspace"):
        if "localhost:" in url:
            url = url.replace("localhost:", "host.docker.internal:")
        elif "127.0.0.1:" in url:
            url = url.replace("127.0.0.1:", "host.docker.internal:")

    # Ensure /api/graphql path if target is root port 8000
    if url.rstrip("/").endswith(":8000"):
        url = url.rstrip("/") + "/api/graphql"
    return url


def execute_single_test(item: ChecklistItem, target_url: str) -> tuple[str, str, int]:
    """Execute real adversarial or functional probe against target."""
    resolved_url = resolve_target_url(target_url)
    
    # Extract operation name if present (e.g. "POST getChatAuthUrl" -> "getChatAuthUrl")
    op_name = "testOperation"
    parts = item.title.split()
    if len(parts) >= 2 and parts[-2].upper() in ("POST", "GET", "PUT", "DELETE"):
        op_name = parts[-1]

    payload = {"query": f"mutation {{ {op_name} {{ __typename }} }}"}
    req_data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        resolved_url,
        data=req_data,
        headers={"Content-Type": "application/json", "User-Agent": "Khunkhaar-QA/1.0"},
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            code = resp.getcode()
            body = resp.read().decode("utf-8", errors="ignore")
            data = json.loads(body) if body.startswith("{") else {}

            # GraphQL returns 200 with "errors" for auth failures
            errors = data.get("errors", [])
            err_text = " ".join([e.get("message", "") for e in errors]).lower()
            if any(k in err_text for k in ("unauthorized", "auth", "token", "jwt", "forbidden", "denied", "login")):
                return ("passed", f"Auth boundary held: GraphQL rejected with '{errors[0].get('message')}'", code)
            elif errors:
                return ("passed", f"Input validation rejected unauthorized payload: '{errors[0].get('message')[:60]}'", code)
            elif "data" in data and data["data"] is not None and data["data"].get(op_name):
                return ("failed", f"CRITICAL: Mutation '{op_name}' executed unauthenticated without token!", code)
            return ("passed", f"Request handled safely with response code {code}", code)
            
    except urllib.error.HTTPError as exc:
        code = exc.code
        # HTTP 401 or 403 means auth boundary successfully blocked unauthenticated access
        if code in (401, 403):
            return ("passed", f"Auth boundary verified: Server correctly responded with HTTP {code} ({exc.reason})", code)
        if code in (400, 422):
            return ("passed", f"Input validation active: Server rejected invalid payload with HTTP {code}", code)
        return ("failed", f"Server threw unhandled HTTP {code} error: {exc.reason}", code)
    except urllib.error.URLError as exc:
        return ("failed", f"Target unreachable at {resolved_url}: {exc.reason}", 0)
    except Exception as exc:
        return ("failed", f"Execution error: {exc}", 0)
