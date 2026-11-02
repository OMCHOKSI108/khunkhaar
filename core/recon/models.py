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


@dataclass
class ProjectRecon:
    project_id: str
    project_name: str
    root_path: str
    primary_language: str
    frameworks: list[str] = field(default_factory=list)
    project_type: str = "web-app"  # web-app, rest-api, graphql-api, fullstack, streamlit, cli, microservice
    endpoints: list[EndpointDef] = field(default_factory=list)
    models: list[ModelDef] = field(default_factory=list)
    ports: list[PortInfo] = field(default_factory=list)
    auth_mechanisms: list[str] = field(default_factory=list)
    integrations: list[str] = field(default_factory=list)
    existing_tests: dict[str, Any] = field(default_factory=dict)
    business_rules: list[dict[str, Any]] = field(default_factory=list)
    documentation_sources: list[str] = field(default_factory=list)
    suggested_target_url: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "project_name": self.project_name,
            "root_path": self.root_path,
            "primary_language": self.primary_language,
            "frameworks": self.frameworks,
            "project_type": self.project_type,
            "endpoints": [e.to_dict() for e in self.endpoints],
            "models": [m.to_dict() for m in self.models],
            "ports": [p.to_dict() for p in self.ports],
            "auth_mechanisms": self.auth_mechanisms,
            "integrations": self.integrations,
            "existing_tests": self.existing_tests,
            "business_rules": self.business_rules,
            "documentation_sources": self.documentation_sources,
            "suggested_target_url": self.suggested_target_url,
        }
