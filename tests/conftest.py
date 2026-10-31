"""Shared fixtures for the KHUNKHAAR infrastructure test-suite.

The browser, API and MCP tests all talk to the local mock application, so the server has to be
running. It is started in-process when the suite is invoked without it rather than skipped,
because a skipped test would let the suite pass while proving nothing.
"""

from __future__ import annotations

import importlib.util
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MOCK_HOST = "127.0.0.1"
MOCK_PORT = 8731
MOCK_BASE = f"http://{MOCK_HOST}:{MOCK_PORT}"


def _load_profile_env() -> None:
    """Load operator-owned credentials from data/profiles/db/*.env the way the launcher does.

    Values are never printed; only names already present in the environment are skipped.
    """
    import os

    for path in sorted((ROOT / "data" / "profiles" / "db").glob("*.env")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_profile_env()


def _port_open(host: str, port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


@pytest.fixture(scope="session")
def mock_server() -> str:
    if not _port_open(MOCK_HOST, MOCK_PORT):
        import uvicorn
        from tools.api.mock_server import app

        config = uvicorn.Config(app, host=MOCK_HOST, port=MOCK_PORT, log_level="warning")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.time() + 20
        while not _port_open(MOCK_HOST, MOCK_PORT):
            if time.time() > deadline:
                pytest.fail("mock API server did not start within 20s")
            time.sleep(0.2)

    import httpx

    response = httpx.get(f"{MOCK_BASE}/health", timeout=5)
    assert response.status_code == 200, f"mock /health returned {response.status_code}"
    return MOCK_BASE


def load_server_module(name: str):
    """Load an MCP server module from its file path - 'mcp-servers' is not a valid package name."""
    path = ROOT / "mcp-servers" / name / "server.py"
    spec = importlib.util.spec_from_file_location(f"khunkhaar_mcp_{name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def browser_mcp():
    return load_server_module("browser")


@pytest.fixture(scope="session")
def api_mcp():
    return load_server_module("api")


@pytest.fixture(scope="session")
def filesystem_mcp():
    return load_server_module("filesystem")
