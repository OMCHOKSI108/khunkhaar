"""Runtime API contract discovery and payload construction.

Agents do not hardcode application endpoints. At run time they fetch the target's OpenAPI
document (when available), reduce it to operations with request schemas, and build payloads from
those schemas. When no document exists, static route intelligence is used instead and probes that
need a contract report that limitation instead of guessing silently.
"""

from __future__ import annotations

import copy
import json
import uuid
from typing import Any

METHODS = ("get", "post", "put", "patch", "delete")


def fetch_spec(client) -> dict[str, Any] | None:
    for path in ("/openapi.json", "/api/openapi.json", "/swagger.json", "/v3/api-docs"):
        try:
            response = client.get(path)
        except Exception:
            continue
        if response.ok and isinstance(response.body, dict) and "paths" in response.body:
            return response.body
    return None


def resolve_ref(doc: dict[str, Any], schema: Any, depth: int = 0) -> dict[str, Any]:
    if depth > 5 or not isinstance(schema, dict):
        return {}
    if "$ref" in schema:
        ref = str(schema["$ref"]).split("/")[-1]
        target = (doc.get("components", {}).get("schemas", {}) or {}).get(ref, {})
        return resolve_ref(doc, target, depth + 1)
    if "allOf" in schema:
        merged: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
        for part in schema["allOf"]:
            resolved = resolve_ref(doc, part, depth + 1)
            merged["properties"].update(resolved.get("properties", {}))
            merged["required"].extend(resolved.get("required", []))
        return merged
    return schema


def operations(doc: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not doc:
        return []
    out: list[dict[str, Any]] = []
    for path, methods in (doc.get("paths") or {}).items():
        if not isinstance(methods, dict):
            continue
        for method, meta in methods.items():
            if method not in METHODS or not isinstance(meta, dict):
                continue
            request_schema: dict[str, Any] = {}
            content = (meta.get("requestBody") or {}).get("content") or {}
            for ctype in ("application/json", "application/x-www-form-urlencoded"):
                if ctype in content:
                    request_schema = resolve_ref(doc, content[ctype].get("schema", {}))
                    break
            params = []
            for source in (meta.get("parameters") or []):
                if isinstance(source, dict):
                    params.append({"name": source.get("name"), "in": source.get("in"),
                                   "required": bool(source.get("required"))})
            response_types: set[str] = set()
            for status, response in (meta.get("responses") or {}).items():
                if not isinstance(response, dict):
                    continue
                for ctype in (response.get("content") or {}):
                    response_types.add(str(ctype).lower())
            out.append({
                "method": method.upper(),
                "path": path,
                "summary": meta.get("summary") or "",
                "operation_id": meta.get("operationId"),
                "params": params,
                "request_schema": request_schema,
                "response_types": sorted(response_types),
            })
    return out


def find_operation(ops: list[dict[str, Any]], method: str | None = None,
                   keywords: tuple[str, ...] = (), *, require_path_param: bool = False,
                   forbid_keywords: tuple[str, ...] = ()) -> dict[str, Any] | None:
    for op in ops:
        if method and op["method"] != method.upper():
            continue
        text = (op["path"] + " " + op["summary"] + " " + str(op.get("operation_id") or "")).lower()
        if keywords and not all(keyword.lower() in text for keyword in keywords):
            continue
        if any(keyword.lower() in text for keyword in forbid_keywords):
            continue
        if require_path_param and "{" not in op["path"]:
            continue
        return op
    return None


def operations_for(ops: list[dict[str, Any]], method: str | None = None,
                   keyword: str | None = None) -> list[dict[str, Any]]:
    result = []
    for op in ops:
        if method and op["method"] != method.upper():
            continue
        if keyword and keyword.lower() not in op["path"].lower():
            continue
        result.append(op)
    return result


def _example(prop: dict[str, Any], invalid: bool) -> Any:
    if "example" in prop and not invalid:
        return prop["example"]
    if "enum" in prop and prop["enum"]:
        return prop["enum"][0]
    kind = prop.get("type")
    if kind == "string":
        return "" if invalid else f"khunkhaar-{uuid.uuid4().hex[:8]}"
    if kind == "integer":
        return -1 if invalid else 10
    if kind == "number":
        return -1.5 if invalid else 10.5
    if kind == "boolean":
        return True
    if kind == "array":
        item = _example(prop.get("items") or {"type": "string"}, invalid)
        return [item]
    if kind == "object":
        return build_payload_from_schema(prop, invalid=invalid)
    return "" if invalid else "khunkhaar"


def build_payload_from_schema(schema: dict[str, Any], *, invalid: bool = False,
                              overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    props = schema.get("properties") or {}
    payload: dict[str, Any] = {}
    for name, prop in props.items():
        payload[name] = _example(prop, invalid)
    if overrides:
        payload.update(overrides)
    return payload


def build_payload(doc: dict[str, Any] | None, operation: dict[str, Any] | None, *,
                  invalid: bool = False, overrides: dict[str, Any] | None = None,
                  unique_field: str | None = None, unique_value: str | None = None) -> dict[str, Any]:
    schema = resolve_ref(doc or {}, (operation or {}).get("request_schema") or {})
    payload = build_payload_from_schema(schema, invalid=invalid, overrides=overrides)
    if unique_field and unique_value:
        payload[unique_field] = unique_value
    return payload


def unique_title(prefix: str = "KH-PROBE") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def find_id(body: Any) -> Any:
    """Extract a resource id from a JSON response without assuming one exact shape."""
    if isinstance(body, dict):
        for key in ("id", "request_id", "record_id", "uuid"):
            if key in body and isinstance(body[key], (int, str)):
                return body[key]
        for value in body.values():
            if isinstance(value, dict):
                found = find_id(value)
                if found is not None:
                    return found
    return None


def collection(body: Any, key_hints: tuple[str, ...] = ("items", "data", "results", "records")) -> list:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in key_hints:
            value = body.get(key)
            if isinstance(value, list):
                return value
        for value in body.values():
            if isinstance(value, list):
                return value
    return []


def fmt_path(path: str, value: Any) -> str:
    for token in ("{id}", "{request_id}", "{item_id}", "{record_id}", "{pk}", "{uuid}"):
        path = path.replace(token, str(value))
    return path


def path_params(op: dict[str, Any]) -> list[str]:
    return [p["name"] for p in op.get("params", []) if p.get("in") == "path"]


def query_params(op: dict[str, Any]) -> list[str]:
    return [p["name"] for p in op.get("params", []) if p.get("in") == "query"]


def request_field_names(operation: dict[str, Any], doc: dict[str, Any] | None) -> list[str]:
    schema = resolve_ref(doc or {}, (operation or {}).get("request_schema") or {})
    return list((schema.get("properties") or {}).keys())


def copy_schema(schema: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(schema)
