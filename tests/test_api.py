import io
import json
import sqlite3
from unittest.mock import Mock

import pytest

import api
import data.utils.database as database


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "api.db"))
    database.setup_database()
    app = api.create_app("test-token")

    def request(method, path, body=None, token="test-token", **environ):
        data = json.dumps(body).encode() if body is not None else b""
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "CONTENT_TYPE": "application/json",
            "CONTENT_LENGTH": str(len(data)),
            "HTTP_AUTHORIZATION": "Bearer " + token,
            "wsgi.input": io.BytesIO(data),
            **environ,
        }
        response = {}

        def start_response(status, headers):
            response["status"] = int(status.split()[0])
            response["headers"] = dict(headers)

        response["body"] = json.loads(b"".join(app(environ, start_response)))
        return response

    return request


@pytest.mark.parametrize(
    ("name", "target", "query", "normalized", "result"),
    [
        ("holehe", "run_holehe", " user@example.com ", "user@example.com", {"output": "report"}),
        ("maigret", "run_maigret", " user ", "user", "report"),
        ("helix", "run_helix", " @user ", "user", "report"),
        ("sherlock", "run_sherlock", " user ", "user", "report"),
    ],
)
def test_create_and_get_run_delegates_to_shared_services(
    client, monkeypatch, name, target, query, normalized, result
):
    integration = getattr(api, name + "_integration")
    runner = Mock(return_value=result)
    monkeypatch.setattr(integration, target, runner)
    response = client("POST", "/runs", {"tool": name.upper(), "query": query, "authorized": True})

    assert response["status"] == 201
    run_id = response["body"]["run_id"]
    assert response["body"]["status"] == "success"
    assert response["headers"]["Location"] == f"/runs/{run_id}"
    runner.assert_called_once_with(normalized)
    saved = client("GET", f"/runs/{run_id}")
    assert saved["status"] == 200
    assert saved["body"] == {
        "run_id": run_id, "tool": name, "query": normalized, "status": "success",
        "result": "report", "error": None,
        "started_at": saved["body"]["started_at"], "finished_at": saved["body"]["finished_at"],
    }
    assert saved["body"]["started_at"] and saved["body"]["finished_at"]
    assert database.list_tool_results()[0][3] == "report"


@pytest.mark.parametrize(
    ("error", "status"),
    [(RuntimeError("unavailable"), "failed"), (TimeoutError("timeout"), "timed_out")],
)
def test_failure_and_timeout_are_recorded(client, monkeypatch, error, status):
    monkeypatch.setattr(api.maigret_integration, "run_maigret", Mock(side_effect=error))
    response = client("POST", "/runs", {"tool": "maigret", "query": "user", "authorized": True})
    assert response["status"] == 201
    assert response["body"]["status"] == status
    saved = client("GET", response["headers"]["Location"])["body"]
    assert saved["status"] == status
    assert saved["result"] is None
    assert saved["error"] == str(error)


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ({}, 400, "invalid_request"),
        ({"tool": "maigret", "query": "user", "authorized": True, "options": {}}, 400, "invalid_request"),
        ({"tool": "amass", "query": "example.com", "authorized": True}, 400, "invalid_tool"),
        ({"tool": "unknown", "query": "user", "authorized": True}, 400, "invalid_tool"),
        ({"tool": [], "query": "user", "authorized": True}, 400, "invalid_tool"),
        ({"tool": "maigret", "query": "user", "authorized": 1}, 403, "authorization_required"),
        ({"tool": "maigret", "query": "--bad value", "authorized": True}, 400, "invalid_query"),
        ({"tool": "holehe", "query": None, "authorized": True}, 400, "invalid_query"),
    ],
)
def test_invalid_requests_never_run(client, monkeypatch, body, status, code):
    execute = Mock()
    monkeypatch.setattr(api.tool_runs, "execute_tool_run_with_id", execute)
    response = client("POST", "/runs", body)
    assert response["status"] == status
    assert response["body"]["error"]["code"] == code
    execute.assert_not_called()
    assert database.query_database("SELECT * FROM ToolRuns") == []


def test_authentication_applies_to_reads_and_writes(client, monkeypatch):
    execute = Mock()
    monkeypatch.setattr(api.tool_runs, "execute_tool_run_with_id", execute)
    for method, path in [("GET", "/runs/1"), ("POST", "/runs")]:
        response = client(method, path, token="wrong")
        assert response["status"] == 401
        assert response["body"]["error"]["code"] == "unauthorized"
        assert response["headers"]["WWW-Authenticate"] == "Bearer"
    execute.assert_not_called()


@pytest.mark.parametrize("token", [None, "", "non-ascii-\u2603"])
def test_app_requires_configured_ascii_token(token):
    with pytest.raises(ValueError):
        api.create_app(token)


def test_request_protocol_errors_and_missing_run(client):
    assert client("POST", "/runs", CONTENT_TYPE="text/plain")["status"] == 415
    assert client("POST", "/runs", CONTENT_LENGTH="bad")["status"] == 400
    assert client("POST", "/runs", CONTENT_LENGTH="4097")["status"] == 413
    assert client("POST", "/runs", CONTENT_LENGTH="2")["body"]["error"]["code"] == "invalid_json"
    assert client("POST", "/runs", **{"wsgi.input": io.BytesIO(b"{"), "CONTENT_LENGTH": "1"})["status"] == 400
    for path in ["/runs/1", "/runs/0", "/runs/-1", "/runs/9223372036854775808", "/runs/1/../../"]:
        assert client("GET", path)["status"] == 404


def test_storage_errors_are_consistent_and_redacted(client, monkeypatch):
    monkeypatch.setattr(database, "start_tool_run", Mock(side_effect=sqlite3.OperationalError("private path")))
    response = client("POST", "/runs", {"tool": "maigret", "query": "user", "authorized": True})
    assert response["status"] == 500
    assert response["body"]["error"]["code"] == "storage_error"
    assert "private path" not in str(response["body"])
    monkeypatch.setattr(database, "get_tool_run", Mock(side_effect=sqlite3.OperationalError("private path")))
    response = client("GET", "/runs/1")
    assert response["status"] == 500
    assert "private path" not in str(response["body"])
