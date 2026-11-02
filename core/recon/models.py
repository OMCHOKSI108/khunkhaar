"""Reconnaissance models for universal project discovery."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PortInfo:
    port: int
    protocol: str = "tcp"
    service_type: str = "unknown"  # http, graphql, database, websocket
    path: str = ""
    status: str = "open"
    banner: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "port": self.port,
            "protocol": self.protocol,
            "service_type": self.service_type,
            "path": self.path,
            "status": self.status,
            "banner": self.banner,
        }
