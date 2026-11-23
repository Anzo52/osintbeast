import json
import sqlite3
import threading
import time
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from unittest.mock import Mock

import pytest

import background_runs
import data.utils.database as database
import web_ui
from sherlock_integration import validate_username


def mocked_worker(connection, tool_name, query, max_output_bytes):
    if query == "slow":
        time.sleep(10)
    elif query == "alice":
        time.sleep(0.3)
        connection.send_bytes(b'Salice: <script>alert("untrusted")</script>')
    elif query == "failure":
        connection.send_bytes(b"Fdependency missing")
    elif query == "unexpected":
        connection.send_bytes(b"Fsherlock failed unexpectedly (Exception).")
    elif tool_name == "holehe":
        connection.send_bytes(b"SFound [redacted]")
    else:
        connection.send_bytes(b"Soutput")
    connection.close()


@pytest.fixture(autouse=True)
def run_service(monkeypatch):
    monkeypatch.setattr(background_runs, "_worker", mocked_worker)
    service = background_runs.BackgroundRuns()
    monkeypatch.setattr(web_ui, "RUN_SERVICE", service)
    yield service
    service.close()


def _request(url, payload=None, headers=None):
    headers = headers or {}
    if payload is None:
        request = Request(url, headers=headers)
    else:
        headers = {
            "Content-Type": "application/json",
            "Origin": f"{urlsplit(url).scheme}://{urlsplit(url).netloc}",
            **headers,
        }
        request = Request(
            url,
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
    try:
        with urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read())
    except HTTPError as response:
        return response.code, json.loads(response.read())


def _server(tmp_path, monkeypatch, runner):
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "web.db"))
    database.setup_database()
    monkeypatch.setattr(
        web_ui,
        "RUNNABLE_TOOLS",
        {"sherlock": ("username", validate_username, runner)},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), web_ui.WebHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}"


def test_authorized_run_and_persisted_result_review(tmp_path, monkeypatch):
    output = '<script>alert("untrusted")</script>'
    server, base_url = _server(tmp_path, monkeypatch, Mock())
    try:
        status, launched = _request(
            f"{base_url}/api/runs",
            {"tool": "sherlock", "input": " alice ", "authorized": True},
        )
        assert status == 202
        assert launched["status"] == "running"
        assert launched["input"] == "alice"

        status, running = _request(f"{base_url}/api/runs/{launched['id']}")
        assert status == 200
        assert running["status"] == "running"
        assert running["input"] == "alice"
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status, detail = _request(f"{base_url}/api/runs/{launched['id']}")
            if detail["status"] != "running":
                break
            time.sleep(0.01)

        assert status == 200
        assert detail["status"] == "success"
        assert detail["input"] == "alice"
        assert detail["result"] == f"alice: {output}"
        assert database.query_database(
            "SELECT toolName, queryText, resultText FROM ToolResults"
        ) == [("sherlock", "alice", f"alice: {output}")]

        status, recent = _request(f"{base_url}/api/runs?limit=1")
        assert status == 200
        assert recent[0]["id"] == launched["id"]
        assert recent[0]["status"] == "success"

        with urlopen(base_url, timeout=2) as response:
            page = response.read().decode()
        assert "Authorization required" in page
        assert 'id="authorized" type="checkbox" required' in page
        assert 'document.querySelector("#detail-output").textContent = run.result' in page
        assert "innerHTML" not in page
    finally:
        server.shutdown()
        server.server_close()


def test_web_run_views_show_migrated_legacy_records(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "web.db"))
    database.setup_database()
    with database.connect_db() as conn:
        conn.execute("DELETE FROM OsintBeastMigrations")
        conn.execute(
            "INSERT INTO ToolRuns (toolName, queryText, status, errorText) "
            "VALUES ('holehe', 'private@example.com', 'failed', 'private%40example.com')"
        )

    server, base_url = _server(tmp_path, monkeypatch, Mock())
    try:
        status, recent = _request(f"{base_url}/api/runs")
        assert status == 200
        assert recent[0]["input"] == "[redacted]"
        status, detail = _request(f"{base_url}/api/runs/{recent[0]['id']}")
        assert status == 200
        assert detail["input"] == "[redacted]"
        assert detail["error"] == "[redacted]"
    finally:
        server.shutdown()
        server.server_close()


def test_unpersisted_completion_is_logged_and_reconciled(tmp_path, monkeypatch, caplog):
    server, base_url = _server(tmp_path, monkeypatch, Mock(return_value="output"))
    finish = database.finish_tool_run
    monkeypatch.setattr(
        database,
        "finish_tool_run",
        Mock(side_effect=sqlite3.OperationalError("database unavailable")),
    )
    try:
        status, launched = _request(
            f"{base_url}/api/runs",
            {"tool": "sherlock", "input": "other", "authorized": True},
        )
        assert status == 202
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not caplog.records:
            time.sleep(0.01)
        assert "Could not persist background run outcome" in caplog.text

        status, _ = _request(f"{base_url}/api/runs/{launched['id']}")
        assert status == 503
        monkeypatch.setattr(database, "finish_tool_run", finish)
        status, detail = _request(f"{base_url}/api/runs/{launched['id']}")
        assert status == 200
        assert detail["status"] == "success"
        assert detail["result"] == "output"
    finally:
        monkeypatch.setattr(database, "finish_tool_run", finish)
        server.shutdown()
        server.server_close()


def test_holehe_email_is_redacted_in_web_ui_history(tmp_path, monkeypatch):
    sensitive_value = "person@example.com"
    server, base_url = _server(
        tmp_path, monkeypatch, Mock(return_value=f"Found {sensitive_value}")
    )
    monkeypatch.setattr(
        web_ui,
        "RUNNABLE_TOOLS",
        {"holehe": ("email", lambda value: value, Mock(return_value=f"Found {sensitive_value}"))},
    )
    try:
        status, launched = _request(
            f"{base_url}/api/runs",
            {"tool": "holehe", "input": sensitive_value, "authorized": True},
        )
        assert status == 202
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status, detail = _request(f"{base_url}/api/runs/{launched['id']}")
            if detail["status"] != "running":
                break
            time.sleep(0.01)

        assert status == 200
        assert detail["input"] == "[redacted]"
        assert detail["result"] == "Found [redacted]"
        assert database.query_database(
            "SELECT queryText, resultText FROM ToolResults"
        ) == [("[redacted]", "Found [redacted]")]
    finally:
        server.shutdown()
        server.server_close()


def test_validation_and_authorization_failures_do_not_launch(
    tmp_path, monkeypatch
):
    runner = Mock(return_value="should not run")
    server, base_url = _server(tmp_path, monkeypatch, runner)
    try:
        cases = [
            (
                {"tool": "sherlock", "input": "bad input!", "authorized": True},
                400,
            ),
            (
                {"tool": "sherlock", "input": "alice", "authorized": False},
                403,
            ),
            (
                {"tool": "amass", "input": "example.com", "authorized": True},
                400,
            ),
        ]
        for payload, expected_status in cases:
            status, body = _request(f"{base_url}/api/runs", payload)
            assert status == expected_status
            assert body["error"]
        status, body = _request(
            f"{base_url}/api/runs",
            {"tool": "sherlock", "input": "alice", "authorized": True},
            {"Origin": "https://attacker.example"},
        )
        assert status == 403
        assert body["error"]
        status, body = _request(
            f"{base_url}/api/runs",
            {"tool": "sherlock", "input": "alice", "authorized": True},
            {"Content-Type": "text/plain"},
        )
        assert status == 415
        assert body["error"]
        status, body = _request(
            f"{base_url}/api/runs",
            {"tool": "sherlock", "input": "alice", "authorized": True},
            {"Host": "attacker.example"},
        )
        assert status == 403
        assert body["error"]
        runner.assert_not_called()
        assert database.query_database("SELECT * FROM ToolRuns") == []

        status, body = _request(f"{base_url}/api/runs/not-a-number")
        assert status == 404
        assert body["error"] == "Run not found."
    finally:
        server.shutdown()
        server.server_close()


def test_terminal_tool_error_is_available_for_review(tmp_path, monkeypatch):
    runner = Mock(side_effect=RuntimeError("dependency missing"))
    server, base_url = _server(tmp_path, monkeypatch, runner)
    try:
        status, launched = _request(
            f"{base_url}/api/runs",
            {"tool": "sherlock", "input": "failure", "authorized": True},
        )
        assert status == 202

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status, detail = _request(f"{base_url}/api/runs/{launched['id']}")
            if detail["status"] != "running":
                break
            time.sleep(0.01)

        assert status == 200
        assert detail["status"] == "failed"
        assert detail["error"] == "dependency missing"
        assert detail["result"] is None
    finally:
        server.shutdown()
        server.server_close()


def test_unexpected_background_error_is_redacted(tmp_path, monkeypatch, caplog):
    runner = Mock(side_effect=Exception("private exception"))
    server, base_url = _server(tmp_path, monkeypatch, runner)
    try:
        status, launched = _request(
            f"{base_url}/api/runs",
            {"tool": "sherlock", "input": "unexpected", "authorized": True},
        )
        assert status == 202
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            _, detail = _request(f"{base_url}/api/runs/{launched['id']}")
            if detail["status"] != "running":
                break
            time.sleep(0.01)
        assert detail["status"] == "failed"
        assert detail["input"] == "unexpected"
        assert detail["error"] == "sherlock failed unexpectedly (Exception)."
    finally:
        server.shutdown()
        server.server_close()
    assert "private exception" not in caplog.text


def test_browser_capacity_rejection_and_recovery(tmp_path, monkeypatch, run_service):
    server, base_url = _server(tmp_path, monkeypatch, Mock())
    payload = {"tool": "sherlock", "input": "slow", "authorized": True}
    try:
        first_status, first = _request(f"{base_url}/api/runs", payload)
        second_status, second = _request(f"{base_url}/api/runs", payload)
        assert first_status == second_status == 202
        assert first["id"] != second["id"]
        status, body = _request(f"{base_url}/api/runs", payload)
        assert status == 503
        assert "capacity" in body["error"]
        assert len(database.list_tool_runs()) == 2
        assert run_service.cancel(first["id"])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if run_service.status(first["id"])["status"] != "running":
                break
            time.sleep(0.01)
        status, third = _request(f"{base_url}/api/runs", payload)
        assert status == 202
        assert third["id"] not in (first["id"], second["id"])
    finally:
        server.shutdown()
        server.server_close()
