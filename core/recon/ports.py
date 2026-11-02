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
