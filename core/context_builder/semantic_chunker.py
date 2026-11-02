"""AST-Aware Semantic Chunker: builds enriched contextual units for vector embedding."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from core.context_builder.models import CodeSymbol


def compute_content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def build_contextual_unit(
    project_name: str,
    file_path: str,
    symbol: CodeSymbol,
    source_lines: list[str],
    dependencies: list[str] | None = None,
) -> dict[str, Any]:
    """Constructs an enriched contextual representation of an AST unit."""
    start = max(1, symbol.start_line)
    end = min(len(source_lines), symbol.end_line)
    code_block = "\n".join(source_lines[start - 1:end]).strip()

    deps_str = "\n".join(f"- {d}" for d in (dependencies or [])[:8]) if dependencies else "- (none)"
    purpose = symbol.docstring or f"{symbol.symbol_type.capitalize()} {symbol.symbol_name}"

    contextual_text = f"""Project: {project_name}
Module: {Path(file_path).parent}
File: {file_path}

Symbol: {symbol.symbol_name}
Type: {symbol.symbol_type.capitalize()}
Purpose: {purpose}

Dependencies:
{deps_str}

Source Code:
{code_block}
"""
    chash = compute_content_hash(code_block)
    return {
        "symbol_name": symbol.symbol_name,
        "symbol_type": symbol.symbol_type,
        "file_path": file_path,
        "line_start": start,
        "line_end": end,
        "content": contextual_text.strip(),
        "raw_code": code_block,
        "content_hash": chash,
        "metadata": {
            "signature": symbol.signature,
            "purpose": purpose,
            "dependencies": dependencies or [],
        },
    }


def chunk_file_by_ast(
    project_name: str,
    file_path: Path,
    rel_path: str,
    symbols: list[CodeSymbol],
    file_imports: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Chunks a file using extracted AST symbols, falling back to clean blocks if no symbols."""
    chunks: list[dict[str, Any]] = []
    try:
        lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return chunks

    if not lines:
        return chunks

    file_syms = [s for s in symbols if s.file_path == rel_path]
    if file_syms:
        for sym in file_syms:
            chunks.append(build_contextual_unit(project_name, rel_path, sym, lines, file_imports))
    else:
        # Fallback to bounded section chunking (300-800 tokens ~ 40 lines)
        for i in range(0, len(lines), 40):
            block = "\n".join(lines[i:i + 45]).strip()
            if len(block) > 50:
                chash = compute_content_hash(block)
                chunks.append({
                    "symbol_name": Path(rel_path).stem,
                    "symbol_type": "file_section",
                    "file_path": rel_path,
                    "line_start": i + 1,
                    "line_end": min(i + 45, len(lines)),
                    "content": f"Project: {project_name}\nFile: {rel_path}\n\n{block}",
                    "raw_code": block,
                    "content_hash": chash,
                    "metadata": {"section_index": i // 40},
                })
    return chunks
