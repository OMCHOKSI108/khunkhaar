"""The optional GitLab adapter: off by default, GET-only, token never persisted.

The adapter is tested against a throwaway HTTP server on loopback. No test reaches a real GitLab,
and the host still has to pass both configuration gates before a socket is opened.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from core.integrations import gitlab

TOKEN_VALUE = "glpat-test-token-value-not-real"


class FakeGitLab:
    """A local HTTP endpoint that answers the two paths the adapter may ask for."""

    def __init__(self):
        self.status_code = 200
        self.requests: list[dict] = []
        self.metadata = {
            "iid": 7,
            "title": "Add the rename method",
            "state": "opened",
            "draft": False,
            "source_branch": "feature/items",
            "target_branch": "main",
            "sha": "a" * 40,
            "web_url": "http://127.0.0.1/group/proj/-/merge_requests/7",
            "author": {"username": "developer"},
            "changes_count": "1",
            "created_at": "2026-10-30T10:00:00Z",
            "updated_at": "2026-10-30T11:00:00Z",
        }
        self.diff_files = [{
            "old_path": "app/service.py", "new_path": "app/service.py",
            "new_file": False, "deleted_file": False, "renamed_file": False,
            "diff": "TOP SECRET DIFF BODY",
        }]
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    @property
    def allowlist_entry(self) -> str:
        return f"127.0.0.1:{self._server.server_address[1]}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server API
                fake.requests.append({"method": "GET", "path": self.path,
                                      "headers": dict(self.headers)})
                if fake.status_code != 200:
                    self._send(fake.status_code, {"message": "denied"})
                    return
                path = self.path.split("?", 1)[0]
                if path.endswith("/diffs"):
                    self._send(200, fake.diff_files)
                elif "/merge_requests/" in path:
                    self._send(200, fake.metadata)
                else:
                    self._send(404, {"message": "not found"})

            def do_POST(self):  # noqa: N802 - exists only to catch a regression
                fake.requests.append({"method": "POST", "path": self.path})
                self._send(405, {"message": "the adapter must never write"})

            def _send(self, code: int, body) -> None:
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args) -> None:
                pass

        return Handler


@pytest.fixture
def gitlab_server():
    server = FakeGitLab()
    yield server
    server.close()


def _enable(patch_config, server: FakeGitLab, *, allow: bool = True) -> None:
    patch_config("gitlab", {
        "enabled": True, "host": server.url, "project": "group/proj",
        "token_env": "KHUNKHAAR_TEST_GITLAB_TOKEN", "timeout_seconds": 2,
        "max_diff_files": 10,
    })
    if allow:
        patch_config("api", {"allowed_hosts": [server.allowlist_entry]})


def test_disabled_by_default_never_constructs_an_http_client(kh_root, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("the adapter opened an HTTP client while disabled")

    monkeypatch.setattr(gitlab.httpx, "Client", explode)
    result = gitlab.fetch_merge_request(7)
    assert result["status"] == gitlab.STATUS_DISABLED
    assert "gitlab.enabled is false" in result["reason"]
    assert result["merge_request"] is None


def test_enabled_without_token_never_sends_a_request(kh_root, gitlab_server, patch_config,
                                                     monkeypatch):
    _enable(patch_config, gitlab_server)
    monkeypatch.delenv("KHUNKHAAR_TEST_GITLAB_TOKEN", raising=False)

    def explode(*args, **kwargs):
        raise AssertionError("a request was attempted without a token")

    monkeypatch.setattr(gitlab.httpx, "Client", explode)
    result = gitlab.fetch_merge_request(7)
    assert result["status"] == gitlab.STATUS_UNAVAILABLE
    assert "KHUNKHAAR_TEST_GITLAB_TOKEN" in result["reason"]
    assert gitlab_server.requests == []


def test_host_must_also_be_in_the_api_allowlist(kh_root, gitlab_server, patch_config,
                                                monkeypatch):
    _enable(patch_config, gitlab_server, allow=False)
    monkeypatch.setenv("KHUNKHAAR_TEST_GITLAB_TOKEN", TOKEN_VALUE)

    def explode(*args, **kwargs):
        raise AssertionError("an unallowlisted host was contacted")

    monkeypatch.setattr(gitlab.httpx, "Client", explode)
    result = gitlab.fetch_merge_request(7)
    assert result["status"] == gitlab.STATUS_REFUSED
    assert "api.allowed_hosts" in result["reason"]
    assert gitlab_server.requests == []


def test_token_comes_from_the_named_env_var_and_is_never_returned(kh_root, gitlab_server,
                                                                   patch_config, monkeypatch):
    _enable(patch_config, gitlab_server)
    monkeypatch.setenv("KHUNKHAAR_TEST_GITLAB_TOKEN", TOKEN_VALUE)

    result = gitlab.fetch_merge_request(7)
    assert result["status"] == gitlab.STATUS_OK, result["reason"]
    assert result["merge_request"]["title"] == "Add the rename method"
    assert result["merge_request"]["source_branch"] == "feature/items"
    assert result["diff"]["files"][0]["new_path"] == "app/service.py"
    assert len(gitlab_server.requests) == 2
    assert all(item["method"] == "GET" for item in gitlab_server.requests)
    assert gitlab_server.requests[0]["headers"].get("PRIVATE-TOKEN") == TOKEN_VALUE
    serialized = json.dumps(result)
    assert TOKEN_VALUE not in serialized, "the token value must never travel in the result"
    assert "TOP SECRET DIFF BODY" not in serialized, "diff text is never stored"


def test_a_401_becomes_unavailable_and_never_raises(kh_root, gitlab_server, patch_config,
                                                    monkeypatch):
    _enable(patch_config, gitlab_server)
    monkeypatch.setenv("KHUNKHAAR_TEST_GITLAB_TOKEN", TOKEN_VALUE)
    gitlab_server.status_code = 401
    result = gitlab.fetch_merge_request(7)
    assert result["status"] == gitlab.STATUS_UNAVAILABLE
    assert "401" in result["reason"]
    assert TOKEN_VALUE not in json.dumps(result)


def test_the_module_contains_no_write_request_path():
    source = Path(gitlab.__file__).read_text(encoding="utf-8")
    for forbidden in (".post(", ".put(", ".patch(", ".delete(", "method=\"POST\""):
        assert forbidden not in source
    assert "client.get(" in source
