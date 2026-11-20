import http.client
import json
import threading

import pytest

import data.utils.database as database
import web_api

OPERATOR_TOKEN = "o" * 40
VIEWER_TOKEN = "v" * 40
TOKENS = {OPERATOR_TOKEN: "operator", VIEWER_TOKEN: "viewer"}


@pytest.fixture
def api_server(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "api.db"))
    database.setup_database()
    monkeypatch.setenv("OSINTBEAST_API_TOKENS", json.dumps(TOKENS))
    server = web_api.create_server(port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize(
    "raw_tokens",
    [
        "{",
        "null",
        "[]",
        '"operator"',
        "1",
        "{}",
        json.dumps({"o" * 31: "operator"}),
        json.dumps({"o" * 257: "operator"}),
        json.dumps({"+" * 40: "operator"}),
        json.dumps({OPERATOR_TOKEN: "admin"}),
        json.dumps({OPERATOR_TOKEN: None}),
        json.dumps({OPERATOR_TOKEN: "operator", VIEWER_TOKEN: "admin"}),
    ],
)
def test_server_rejects_invalid_environment_tokens_before_binding(
    monkeypatch, raw_tokens
):
    monkeypatch.setenv("OSINTBEAST_API_TOKENS", raw_tokens)

    def fail_if_bound(*args, **kwargs):
        pytest.fail("Invalid token configuration must fail before binding.")

    monkeypatch.setattr(web_api, "ThreadingHTTPServer", fail_if_bound)
    with pytest.raises(ValueError):
        web_api.create_server(port=0)


def send_request(server, method, path, token=None, payload=None):
    connection = http.client.HTTPConnection(*server.server_address)
    headers = {}
    body = None
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload)
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    result = json.loads(response.read())
    connection.close()
    return response.status, result


def test_server_requires_tokens_and_rejects_non_loopback_bind(monkeypatch):
    monkeypatch.delenv("OSINTBEAST_API_TOKENS", raising=False)
    with pytest.raises(ValueError, match="must be configured"):
        web_api.create_server()
    with pytest.raises(ValueError, match="loopback"):
        web_api.create_server(host="0.0.0.0", tokens=TOKENS)


def test_api_rejects_missing_and_invalid_credentials(api_server):
    assert send_request(api_server, "GET", "/api/results") == (
        401, {"error": "Authentication required."}
    )
    assert send_request(api_server, "GET", "/api/runs", token="invalid") == (
        401, {"error": "Authentication required."}
    )
    assert send_request(
        api_server,
        "POST",
        "/api/runs",
        payload={"tool": "holehe", "query": "person@example.com"},
    ) == (401, {"error": "Authentication required."})
    assert database.query_database("SELECT * FROM ToolRuns") == []


def test_viewer_can_read_but_cannot_launch_runs(api_server, monkeypatch):
    monkeypatch.setitem(
        web_api.RUNNERS, "holehe", (lambda value: value, lambda value: "checked")
    )
    status, response = send_request(
        api_server,
        "POST",
        "/api/runs",
        token=VIEWER_TOKEN,
        payload={"tool": "holehe", "query": "person@example.com"},
    )
    assert status == 403
    assert response == {"error": "Insufficient permission."}
    assert database.query_database("SELECT * FROM ToolRuns") == []

    assert send_request(api_server, "GET", "/api/results", token=VIEWER_TOKEN) == (
        200, {"results": []}
    )


def test_operator_can_launch_and_read_runs_and_results(api_server, monkeypatch):
    monkeypatch.setitem(
        web_api.RUNNERS, "holehe", (lambda value: value, lambda value: "checked")
    )
    status, response = send_request(
        api_server,
        "POST",
        "/api/runs",
        token=OPERATOR_TOKEN,
        payload={"tool": "holehe", "query": "person@example.com"},
    )
    assert status == 201
    assert response == {"status": "success", "result": "checked"}

    status, runs = send_request(api_server, "GET", "/api/runs", token=OPERATOR_TOKEN)
    assert status == 200
    assert runs["runs"][0]["tool"] == "holehe"
    assert runs["runs"][0]["status"] == "success"

    status, results = send_request(
        api_server, "GET", "/api/results", token=OPERATOR_TOKEN
    )
    assert status == 200
    assert results["results"][0]["result"] == "checked"
