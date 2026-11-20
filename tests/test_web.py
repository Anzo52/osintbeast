from unittest.mock import Mock

import data.utils.database as database
import pytest
import web


TOKEN = "test-token"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "web.db"))
    database.setup_database()
    app = web.create_app({"TESTING": True, "API_TOKEN": TOKEN})
    return app.test_client()


def auth_headers(token=TOKEN):
    return {"Authorization": "Bearer " + token}


def test_launch_uses_shared_run_service_and_hides_input(client, monkeypatch):
    service = Mock(return_value=(42, "success", "sensitive output"))
    monkeypatch.setattr(web, "execute_tool_run_with_id", service)

    response = client.post(
        "/api/runs",
        json={"tool": "holehe", "query": "person@example.com"},
        headers=auth_headers(),
    )

    assert response.status_code == 201
    assert response.json == {"run_id": 42, "status": "success"}
    service.assert_called_once()
    tool, query, runner = service.call_args.args
    assert (tool, query) == ("holehe", "person@example.com")
    assert runner is web.TOOLS["holehe"][1]
    assert b"person@example.com" not in response.data


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"tool": "unknown", "query": "person@example.com"},
        {"tool": "holehe", "query": "not-an-email"},
        {"tool": "holehe", "query": "private@example.com", "extra": "value"},
        ["holehe", "person@example.com"],
    ],
)
def test_invalid_launch_does_not_run_tools(client, monkeypatch, payload):
    monkeypatch.setattr(
        web,
        "execute_tool_run_with_id",
        lambda *args: pytest.fail("invalid request launched a tool"),
    )

    response = client.post("/api/runs", json=payload, headers=auth_headers())

    assert response.status_code == 400
    assert response.json == {"error": "Invalid request."}
    assert b"private@example.com" not in response.data


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/api/runs", "post"),
        ("/api/runs/1", "get"),
        ("/api/results/1", "get"),
    ],
)
@pytest.mark.parametrize("token", [None, "wrong-token"])
def test_protected_routes_reject_unauthenticated_requests(
    client, monkeypatch, path, method, token
):
    monkeypatch.setattr(
        web,
        "execute_tool_run_with_id",
        lambda *args: pytest.fail("unauthorized request launched a tool"),
    )
    headers = auth_headers(token) if token else {}

    response = getattr(client, method)(
        path, json={"tool": "holehe", "query": "private@example.com"}, headers=headers
    )

    assert response.status_code == 401
    assert response.json == {"error": "Authentication required."}
    assert b"private@example.com" not in response.data


def test_missing_server_token_fails_closed():
    client = web.create_app({"TESTING": True, "API_TOKEN": None}).test_client()

    response = client.get("/api/runs/1")

    assert response.status_code == 503
    assert response.json == {"error": "Service unavailable."}


def test_status_route_returns_only_public_status_fields(client, monkeypatch):
    monkeypatch.setattr(
        database,
        "get_tool_run",
        lambda run_id: {
            "run_id": run_id,
            "tool": "holehe",
            "status": "failed",
            "started_at": "2026-01-01",
            "finished_at": "2026-01-01",
            "query": "private@example.com",
            "error": "internal exception",
        },
    )

    response = client.get("/api/runs/7", headers=auth_headers())

    assert response.status_code == 200
    assert response.json == {
        "run_id": 7,
        "tool": "holehe",
        "status": "failed",
        "started_at": "2026-01-01",
        "finished_at": "2026-01-01",
    }
    assert b"private@example.com" not in response.data
    assert b"internal exception" not in response.data


def test_result_route_returns_saved_result_without_query_text(client, monkeypatch):
    monkeypatch.setattr(
        database,
        "get_tool_result",
        lambda result_id: {
            "result_id": result_id,
            "tool": "holehe",
            "result": "scan output",
            "created_at": "2026-01-01",
            "query": "private@example.com",
        },
    )

    response = client.get("/api/results/9", headers=auth_headers())

    assert response.status_code == 200
    assert response.json == {
        "result_id": 9,
        "tool": "holehe",
        "result": "scan output",
        "created_at": "2026-01-01",
    }
    assert b"private@example.com" not in response.data


def test_unexpected_service_failure_does_not_leak_details(client, monkeypatch):
    monkeypatch.setattr(
        web,
        "execute_tool_run_with_id",
        lambda *args: (_ for _ in ()).throw(
            RuntimeError("internal detail: person@example.com")
        ),
    )

    response = client.post(
        "/api/runs",
        json={"tool": "holehe", "query": "person@example.com"},
        headers=auth_headers(),
    )

    assert response.status_code == 500
    assert response.json == {"error": "Run could not be completed."}
    assert b"internal detail" not in response.data
    assert b"person@example.com" not in response.data
