import io
import json
import os
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
        "run_id": run_id, "tool": name,
        "query": "[redacted]",
        "status": "success",
        "result": "report", "error": None,
        "started_at": saved["body"]["started_at"], "finished_at": saved["body"]["finished_at"],
    }
    assert saved["body"]["started_at"] and saved["body"]["finished_at"]
    assert database.list_tool_results()[0][3] == "report"


def test_api_redacts_sensitive_input_and_output(client, monkeypatch):
    sensitive_value = "person@example.com"
    monkeypatch.setattr(
        api.holehe_integration,
        "run_holehe",
        Mock(return_value={"output": f"Found {sensitive_value}"}),
    )

    response = client(
        "POST",
        "/runs",
        {"tool": "holehe", "query": sensitive_value, "authorized": True},
    )
    run_id = response["body"]["run_id"]
    saved = client("GET", f"/runs/{run_id}")["body"]

    assert saved["query"] == "[redacted]"
    assert saved["result"] == "Found [redacted]"
    assert database.list_tool_results()[0][2:4] == (
        "[redacted]", "Found [redacted]"
    )


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


def test_unexpected_runner_failure_is_recorded_without_leaking_details(client, monkeypatch):
    monkeypatch.setattr(api.maigret_integration, "run_maigret", Mock(side_effect=TypeError("private")))
    response = client("POST", "/runs", {"tool": "maigret", "query": "user", "authorized": True})
    assert response["status"] == 500
    assert response["body"]["error"]["code"] == "internal_error"
    saved = client("GET", "/runs/1")["body"]
    assert saved["status"] == "failed"
    assert saved["error"] == "maigret failed unexpectedly (TypeError)."
    assert saved["result"] is None


def test_unexpected_sensitive_failure_is_logged_without_input(client, monkeypatch, caplog):
    sensitive_value = "person@example.com"
    monkeypatch.setattr(
        api.holehe_integration,
        "run_holehe",
        Mock(side_effect=TypeError(f"broken lookup for {sensitive_value}")),
    )

    response = client(
        "POST", "/runs",
        {"tool": "holehe", "query": sensitive_value, "authorized": True},
    )

    assert response["status"] == 500
    assert response["body"]["error"]["code"] == "internal_error"
    assert sensitive_value not in caplog.text
    assert "TypeError" in caplog.text
    saved = client("GET", "/runs/1")["body"]
    assert saved["query"] == "[redacted]"
    assert saved["error"] == "holehe failed unexpectedly (TypeError)."


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


@pytest.mark.parametrize("scheme", ["bearer", "BEARER"])
def test_authentication_scheme_is_case_insensitive(client, monkeypatch, scheme):
    execute = Mock(return_value=(1, "success", None))
    monkeypatch.setattr(api.tool_runs, "execute_tool_run_with_id", execute)
    response = client(
        "POST", "/runs", {"tool": "maigret", "query": "user", "authorized": True},
        HTTP_AUTHORIZATION=f"{scheme} test-token",
    )
    assert response["status"] == 201


def test_factory_initializes_database_and_private_permissions(tmp_path, monkeypatch):
    db_path = tmp_path / "fresh.db"
    monkeypatch.setattr(database, "DB_FILE", str(db_path))
    api.create_app("test-token")
    assert db_path.exists()
    assert os.stat(db_path).st_mode & 0o777 == 0o600
    assert database.query_database("SELECT name FROM sqlite_master WHERE type = 'table'")


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
    nested = b"[" * 1500 + b"]" * 1500
    response = client("POST", "/runs", **{"wsgi.input": io.BytesIO(nested), "CONTENT_LENGTH": str(len(nested))})
    assert response["body"]["error"]["code"] in {"invalid_json", "invalid_request"}
    for path in ["/runs/1", "/runs/0", "/runs/-1", "/runs/9223372036854775808", "/runs/1/../../"]:
        assert client("GET", path)["status"] == 404


def test_json_recursion_error_is_reported_as_invalid_json(monkeypatch):
    def fail(_):
        raise RecursionError()

    monkeypatch.setattr(api.json, "loads", fail)
    app = api.create_app("test-token")
    response = {}
    environ = {
        "REQUEST_METHOD": "POST", "PATH_INFO": "/runs",
        "CONTENT_TYPE": "application/json", "CONTENT_LENGTH": "2",
        "HTTP_AUTHORIZATION": "Bearer " + "test-token", "wsgi.input": io.BytesIO(b"{}"),
    }
    body = b"".join(app(environ, lambda status, headers: response.update(status=status)))
    assert response["status"].startswith("400")
    assert b'"invalid_json"' in body


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


def test_unexpected_service_errors_are_redacted(client, monkeypatch):
    monkeypatch.setattr(api.tool_runs, "execute_tool_run_with_id", Mock(side_effect=TypeError("private")))
    response = client("POST", "/runs", {"tool": "maigret", "query": "user", "authorized": True})
    assert response["status"] == 500
    assert response["body"]["error"]["code"] == "internal_error"
    assert "private" not in str(response["body"])
