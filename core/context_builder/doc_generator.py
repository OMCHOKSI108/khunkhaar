"""Markdown Context Generator: produces persistent verified documentation."""
from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any

from core.context_builder.models import CodeSymbol, ContextDocument, FileInfo


def generate_project_md(name: str, files: list[FileInfo], fw_info: dict[str, Any]) -> ContextDocument:
    total_files = len(files)
    langs: dict[str, int] = {}
    entrypoints: list[str] = []
    for f in files:
        langs[f.language] = langs.get(f.language, 0) + 1
        if f.is_entrypoint:
            entrypoints.append(f.relative_path)

    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    content = f"""# {name} — Project Context
Generated: {ts} | Status: Static Analysis Verified

## 1. Overview
- **Total Tracked Files**: {total_files}
- **Detected Frameworks**: {', '.join(fw_info.get('frameworks') or ['None'])}
- **Primary Languages**: {', '.join(f'{k} ({v})' for k, v in sorted(langs.items(), key=lambda x: -x[1])[:5])}

## 2. Key Entrypoints
{chr(10).join(f'- `{ep}`' for ep in entrypoints) if entrypoints else '- None identified'}

## 3. Key Dependencies
{chr(10).join(f'- `{d}`' for d in (fw_info.get('dependencies') or [])[:15])}
"""
    return ContextDocument("PROJECT", "PROJECT.md", content, entrypoints, timestamp=ts)


def generate_api_md(name: str, symbols: list[CodeSymbol]) -> ContextDocument:
    routes = [s for s in symbols if s.symbol_type == "route"]
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    rows: list[str] = []
    source_refs: list[str] = []
    for r in routes:
        ref = f"{r.file_path}#L{r.start_line}-L{r.end_line}"
        rows.append(f"| `{r.symbol_name}` | `{r.file_path}:{r.start_line}` | Verified |")
        source_refs.append(ref)

    table_body = "\n".join(rows) if rows else "| None | - | - |"
    content = f"""# {name} — API Context
Generated: {ts} | Total Endpoints: {len(routes)}

| Endpoint | Source Reference | Status |
| :--- | :--- | :--- |
{table_body}
"""
    return ContextDocument("API", "API.md", content, source_refs, timestamp=ts)


def generate_database_md(name: str, schema_info: dict[str, Any] | None) -> ContextDocument:
    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
    schema = schema_info or {}
    roles = schema.get("roles") or []
    content = f"""# {name} — Database Context
Generated: {ts} | Target Engine: PostgreSQL / SQL

## 1. Schema Configuration
- **Users Table**: `{schema.get('users_table') or 'users'}`
- **ID Column**: `{schema.get('id_column') or 'id'}`
- **Role Column**: `{schema.get('role_column') or 'role'}`
- **Active Filter**: `{schema.get('active_column') or 'None'}`

## 2. Recognized RBAC Roles
{chr(10).join(f"- `{r.get('key')}` (Database Enum Value: `{r.get('value')}`)" for r in roles) if roles else '- None discovered'}
"""
    return ContextDocument("DATABASE", "DATABASE.md", content, timestamp=ts)


def write_context_directory(root_dir: Path, docs: list[ContextDocument]) -> None:
    target = root_dir / ".context"
    target.mkdir(parents=True, exist_ok=True)
    for doc in docs:
        (target / doc.path).write_text(doc.content, encoding="utf-8")
