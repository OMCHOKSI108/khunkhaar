"""Sensitive-column masking applied to every result the DB tool returns.

Two mechanisms, both mandatory:
  * explicit per-profile lists ("users.email", "orders.card_last4")
  * name-based detection via the shared redaction rules, so an unlisted column like
    "users.password_reset_token" is still masked.

Masking happens before results leave the adapter layer, so it also covers evidence files.
"""

from __future__ import annotations

from typing import Any

from core.shared.redaction import REDACTED, is_secret_key


def _column_rules(profile: dict[str, Any]) -> tuple[set[str], set[str]]:
    qualified: set[str] = set()
    bare: set[str] = set()
    for entry in profile.get("redacted_columns") or []:
        text = str(entry).strip().lower()
        if not text:
            continue
        if "." in text:
            qualified.add(text)
        else:
            bare.add(text)
    return qualified, bare


def mask_rows(rows: list[dict[str, Any]], tables: list[str], profile: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    qualified, bare = _column_rules(profile)
    lower_tables = {t.strip().lower().split(".")[-1] for t in tables if t}
    masked: list[str] = []

    for row in rows:
        for key in list(row.keys()):
            lname = str(key).lower()
            hit = lname in bare or any(lname == q.split(".", 1)[1] for q in qualified if q.split(".", 1)[1] == lname and q.split(".", 1)[0] in lower_tables)
            if not hit and is_secret_key(str(key)):
                hit = True
            if hit and row[key] not in (None, REDACTED):
                row[key] = REDACTED
                if lname not in masked:
                    masked.append(lname)
    return rows, masked


def mask_value(column: str, value: Any, profile: dict[str, Any]) -> Any:
    qualified, bare = _column_rules(profile)
    lname = str(column).lower()
    if lname in bare or is_secret_key(lname) or any(lname == q.split(".", 1)[1] for q in qualified if "." in q):
        return REDACTED
    return value
