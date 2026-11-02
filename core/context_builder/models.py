"""Data models for Tool Context Builder."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FileInfo:
    path: str
    relative_path: str
    size_bytes: int
    language: str
    is_entrypoint: bool = False
    content_hash: str = ""


@dataclass
class CodeSymbol:
    id: str
    project_id: str
    symbol_name: str
    symbol_type: str  # function, class, method, interface, route, model
    file_path: str
    start_line: int
    end_line: int
    signature: str = ""
    docstring: str = ""


@dataclass
class CodeRelationship:
    id: str
    project_id: str
    source_symbol: str
    target_symbol: str
    relationship_type: str  # CALLS, IMPORTS, EXTENDS, USES_MODEL, MAPS_TO_ROUTE
    file_path: str = ""


@dataclass
class ContextDocument:
    doc_type: str  # PROJECT, ARCHITECTURE, API, DATABASE, MODULES
    path: str
    content: str
    source_references: list[str] = field(default_factory=list)
    commit_sha: str = ""
    timestamp: str = ""


@dataclass
class SearchResult:
    file_path: str
    start_line: int
    end_line: int
    content: str
    score: float
    match_type: str  # vector, keyword, symbol, hybrid
    symbol_name: str = ""


@dataclass
class AssembledContext:
    project_id: str
    task: str
    project_summary: str
    relevant_files: list[str] = field(default_factory=list)
    relevant_symbols: list[dict[str, Any]] = field(default_factory=list)
    relationships: list[dict[str, Any]] = field(default_factory=list)
    database_schema: list[dict[str, Any]] = field(default_factory=list)
    code_snippets: list[dict[str, Any]] = field(default_factory=list)
    context_tokens: int = 0
    missing_information: list[str] = field(default_factory=list)
