"""Unit tests for Khunkhaar Web Platform server, store, and routes."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from core.server import agent, runner, store
from core.server.app import create_web_app
from core.server.models import ChatMessage, ChecklistItem, ProjectRecord


@pytest.fixture(autouse=True)
def setup_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_platform.sqlite3"
    monkeypatch.setattr(store, "DB_PATH", test_db)
    store.init_db()


def test_project_store_crud():
    p = ProjectRecord(id="p1", name="TestApp", tags=["GraphQL", "Express"])
    store.save_project(p)
    loaded = store.get_project("p1")
    assert loaded is not None
    assert loaded.name == "TestApp"
    assert loaded.tags == ["GraphQL", "Express"]
    assert len(store.get_projects()) == 1


def test_chat_message_store():
    msg = ChatMessage(project_id="p1", sender="user", text="hello QA")
    store.add_chat_message(msg)
    history = store.get_chat_messages("p1")
    assert len(history) == 1
    assert history[0].text == "hello QA"


def test_checklist_store():
    items = [
        ChecklistItem(id="c1", project_id="p1", category="cyber", title="SQLi test"),
        ChecklistItem(id="c2", project_id="p1", category="rbac", title="Pod Lead approval")
    ]
    store.save_checklist("p1", items)
    loaded = store.get_checklist("p1")
    assert len(loaded) == 2
    assert loaded[0].category == "cyber"


def test_find_free_port():
    port = runner.find_free_port(9000, 9100)
    assert 9000 <= port < 9100


def test_compact_checklist_builder():
    endpoints = [{"name": "login", "path": "/login", "method": "POST"}]
    models = ["User", "Order"]
    items = agent.build_compact_checklist("MyApp", endpoints, models)
    assert len(items) >= 3
    assert any(i["category"] == "cyber" for i in items)
    assert any(i["category"] == "rbac" for i in items)


def test_web_routes_integration():
    app = create_web_app()
    client = TestClient(app)

    # Home
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Hello, This is" in resp.text

    # Create project
    create_resp = client.post("/api/projects", json={
        "name": "LiveApp", "root_path": "/tmp", "tags": ["FastAPI"]
    })
    assert create_resp.status_code == 200
    p_id = create_resp.json()["project"]["id"]

    # Project page
    proj_resp = client.get(f"/projects/{p_id}")
    assert proj_resp.status_code == 200
    assert "LiveApp" in proj_resp.text
