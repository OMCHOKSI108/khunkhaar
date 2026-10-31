"""Read-only SQL validation.

This is the first of three independent layers that keep the DB tool read-only
(the other two are a genuinely read-only database role and a server-side
`SET LOCAL transaction_read_only = on`). Layering matters: if any one of them is
misconfigured, the others still refuse the write.

Rejects:
  * multiple statements (no stacked `SELECT 1; DROP TABLE users`)
  * comments used to smuggle keywords
  * anything that is not SELECT / WITH ... SELECT / EXPLAIN
  * data-modifying CTEs (`WITH x AS (INSERT ... RETURNING *)`)
  * read-only-violating functions (pg_sleep beyond budget, lo_import, dblink, COPY)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_ALLOWED_HEADS = ("select", "with", "explain", "show", "values")

_FORBIDDEN_WORDS = {
    "insert", "update", "delete", "merge", "upsert", "truncate", "drop", "create",
    "alter", "grant", "revoke", "copy", "vacuum", "analyze", "refresh", "call",
    "do", "set", "reset", "lock", "listen", "notify", "unlisten", "reload",
    "prepare", "deallocate", "cluster", "reindex", "discard", "checkpoint",
    "comment", "import", "export", "attach", "detach", "insert overwrite",
}

_FORBIDDEN_PATTERNS = [
    (re.compile(r"\binto\s+(?:outdump|outfile|dumpfile)\b", re.I), "exporting query results to a file"),
    (re.compile(r"\bpg_sleep\s*\(", re.I), "pg_sleep is blocked (timing side-channel / resource abuse)"),
    (re.compile(r"\blo_(?:import|export|open)\s*\(", re.I), "large object access"),
    (re.compile(r"\bdblink(?:_exec)?\s*\(", re.I), "dblink escapes the read-only boundary"),
    (re.compile(r"\bpg_read_(?:file|binary_file)\s*\(", re.I), "filesystem read from SQL"),
    (re.compile(r"\bpg_ls_(?:dir|tmpdir)\s*\(", re.I), "filesystem listing from SQL"),
    (re.compile(r"\bset_config\s*\(\s*['\"]session", re.I), "session-level set_config"),
    (re.compile(r"\bpivot\b|\bcall\s+\w+", re.I), "non-select construct"),
]

# A WITH ... SELECT is legal, but a data-modifying CTE is not.
_DATA_CHANGING_CTE = re.compile(r"\bas\s*\(\s*(?:with\b)?\s*(insert|update|delete|merge)\b", re.I)

_STATEMENT_SPLIT = re.compile(r";\s*")


@dataclass
class Verdict:
    allowed: bool
    reason: str
    kind: str = ""


def strip_comments(sql: str) -> str:
    without_block = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    without_line = re.sub(r"--[^\n]*", " ", without_block)
    return without_line


def _normalize(sql: str) -> str:
    return re.sub(r"\s+", " ", strip_comments(sql)).strip().rstrip(";").strip()


def validate_read_only(sql: str, max_chars: int = 8000) -> Verdict:
    if not sql or not sql.strip():
        return Verdict(False, "empty statement")
    if len(sql) > max_chars:
        return Verdict(False, f"statement is {len(sql)} chars, limit is {max_chars}", "too_long")

    cleaned = strip_comments(sql)
    if cleaned.count("'") % 2 != 0:
        return Verdict(False, "unbalanced string literal - refusing to guess intent", "syntax")

    body = _normalize(sql)
    if not body:
        return Verdict(False, "statement contains only comments/whitespace", "empty")

    statements = [s for s in _STATEMENT_SPLIT.split(body) if s.strip()]
    if len(statements) > 1:
        return Verdict(False, "multiple statements are not permitted in read-only mode", "stacking")

    head = body.split(" ", 1)[0].lower()
    if head not in _ALLOWED_HEADS:
        return Verdict(False, f"statement starts with '{head}'; only {', '.join(_ALLOWED_HEADS)} are read-only safe", "method")

    lowered = " " + body.lower() + " "
    for word in _FORBIDDEN_WORDS:
        pattern = r"\b" + re.escape(word) + r"\b"
        if head == "with" and word in ("select",):
            continue
        if re.search(pattern, lowered):
            # 'create'/'set'/'update' legitimately appear inside identifiers; disallow conservatively
            return Verdict(False, f"keyword '{word}' is not permitted in read-only mode", "keyword")

    m = _DATA_CHANGING_CTE.search(lowered)
    if m:
        return Verdict(False, f"data-modifying CTE ({m.group(1)}) is not permitted", "cte")

    for pattern, description in _FORBIDDEN_PATTERNS:
        if pattern.search(body):
            return Verdict(False, description, "dangerous_function")

    kind = head
    if head == "with":
        kind = "select"
    return Verdict(True, "read-only select", kind)


def extract_tables(sql: str) -> list[str]:
    """Best-effort table list, used for evidence metadata and relation-aware redaction."""
    body = _normalize(sql)
    refs = re.findall(r"\b(?:from|join)\s+([a-zA-Z_][\w.]*|\"[^\"]+\")", body, flags=re.I)
    out: list[str] = []
    for ref in refs:
        name = ref.strip('"')
        if name.lower() in ("select", "unnest", "lateral", "generate_series"):
            continue
        if name not in out:
            out.append(name)
    return out


def add_limit(sql: str, max_rows: int) -> str:
    """Append a hard LIMIT unless the caller already bounded the result set.

    Only applied to plain SELECT/WITH statements; EXPLAIN and SHOW must not be rewritten.
    """
    body = _normalize(sql)
    head = body.split(" ", 1)[0].lower()
    if head not in ("select", "with", "values"):
        return body
    if re.search(r"\blimit\b", body, flags=re.I):
        return body
    if re.search(r"\bfor\s+(?:no\s+key\s+)?update\b", body, flags=re.I):
        return body
    return f"{body} LIMIT {int(max_rows)}"
