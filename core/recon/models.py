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


@dataclass
class EndpointDef:
    name: str
    kind: str  # rest, graphql_query, graphql_mutation, websocket, page
    path: str
    method: str = "GET"
    source_file: str = ""
    line: int = 0
    auth_required: bool = False
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "path": self.path,
            "method": self.method,
            "source_file": self.source_file,
            "line": self.line,
            "auth_required": self.auth_required,
            "description": self.description,
        }


@dataclass
class ModelDef:
    name: str
    table_name: str
    fields: list[str] = field(default_factory=list)
    relationships: list[str] = field(default_factory=list)
    source_file: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "table_name": self.table_name,
            "fields": self.fields,
            "relationships": self.relationships,
            "source_file": self.source_file,
        }
