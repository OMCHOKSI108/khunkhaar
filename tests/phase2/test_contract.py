"""Runtime contract reduction and payload construction (incl. the redaction regression)."""

from __future__ import annotations

from core.missions import spec


DOC = {
    "components": {"schemas": {
        "RequestIn": {"type": "object", "properties": {
            "title": {"type": "string"}, "amount": {"type": "number"}}},
        "LoginIn": {"type": "object", "properties": {
            "username": {"type": "string"}, "password": {"type": "string"}}},
    }},
    "paths": {
        "/api/login": {"post": {
            "summary": "Login",
            "requestBody": {"content": {"application/json": {
                "schema": {"$ref": "#/components/schemas/LoginIn"}}}},
            "responses": {"200": {"content": {"application/json": {"schema": {}}}}},
        }},
        "/api/requests": {
            "get": {"summary": "List", "responses": {
                "200": {"content": {"application/json": {"schema": {}}}}}},
            "post": {
                "summary": "Create",
                "requestBody": {"content": {"application/json": {
                    "schema": {"$ref": "#/components/schemas/RequestIn"}}}},
                "responses": {"201": {"content": {"application/json": {"schema": {}}}}},
            },
        },
        "/requests-page": {"get": {"responses": {
            "200": {"content": {"text/html": {"schema": {}}}}}}},
    },
}


def test_operations_capture_method_path_schema_and_response_types():
    ops = spec.operations(DOC)
    create = next(op for op in ops if op["method"] == "POST" and op["path"] == "/api/requests")
    assert create["request_schema"]["properties"]["title"] == {"type": "string"}
    assert create["response_types"] == ["application/json"]
    page = next(op for op in ops if op["path"] == "/requests-page")
    assert page["response_types"] == ["text/html"]


def test_login_schema_survives_with_its_property_shape():
    """Regression: body redaction used to replace the password schema with a string,
    which broke every probe that logged in."""
    ops = spec.operations(DOC)
    login = next(op for op in ops if op["path"] == "/api/login")
    password = login["request_schema"]["properties"]["password"]
    assert isinstance(password, dict) and password["type"] == "string"


def test_build_payload_resolves_refs_and_applies_overrides():
    ops = spec.operations(DOC)
    login = next(op for op in ops if op["path"] == "/api/login")
    payload = spec.build_payload(DOC, login, overrides={"username": "alice", "password": "x"})
    assert payload == {"username": "alice", "password": "x"}
    request = next(op for op in ops if op["method"] == "POST" and op["path"] == "/api/requests")
    assert spec.build_payload(DOC, request, invalid=True) == {"title": "", "amount": -1.5}


def test_helpers_extract_ids_collections_and_fill_paths():
    assert spec.find_id({"data": {"record_id": 7}}) == 7
    assert spec.collection({"items": [{"id": 1}]}) == [{"id": 1}]
    assert spec.fmt_path("/api/requests/{request_id}/approve", 4) == "/api/requests/4/approve"
    assert spec.request_field_names({"request_schema": {
        "properties": {"a": {}, "b": {}}}}, None) == ["a", "b"]
