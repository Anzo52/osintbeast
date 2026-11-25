import json
import sqlite3
import threading
import time
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import pytest

import background_runs
import data.utils.database as database
import web_ui
from sherlock_integration import validate_username


def mocked_worker(connection, tool_name, query, max_output_bytes, deadline=None):
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
    headers = {"Authorization": "Bearer " + "operator-secret", **headers}
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
    monkeypatch.setenv("OSINTBEAST_OPERATOR_TOKEN", "operator-secret")
    monkeypatch.setenv("OSINTBEAST_VIEWER_TOKEN", "viewer-secret")
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "web.db"))
    monkeypatch.setattr(
        web_ui,
        "RUNNABLE_TOOLS",
        {"sherlock": ("username", validate_username, runner)},
    )
    server = web_ui.create_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}"


def test_serve_shutdown_marks_active_runs_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "shutdown.db"))
    database.setup_database()
    server = Mock(server_port=8000)
    monkeypatch.setattr(web_ui, "create_server", Mock(return_value=server))
    launched = []

    def launch_and_interrupt():
        launched.append(web_ui.RUN_SERVICE.submit("sherlock", "slow"))
        raise KeyboardInterrupt

    server.serve_forever.side_effect = launch_and_interrupt
    web_ui.serve()
    run = database.get_tool_run(launched[0])
    assert run[3] == "failed"
    assert run[5] == "Run cancelled."
    assert run[7] is not None
    server.server_close.assert_called_once()


def test_serve_can_restart_in_the_same_process(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "restart.db"))
    database.setup_database()
    server = Mock(server_port=8000)
    monkeypatch.setattr(web_ui, "create_server", Mock(return_value=server))
    services, run_ids = [], []

    def launch_and_interrupt():
        services.append(web_ui.RUN_SERVICE)
        run_ids.append(web_ui.RUN_SERVICE.submit("sherlock", "slow"))
        raise KeyboardInterrupt

    server.serve_forever.side_effect = launch_and_interrupt
    web_ui.serve()
    web_ui.serve()
    assert services[0] is not services[1]
    assert len(set(run_ids)) == 2
    assert all(database.get_tool_run(run_id)[3] == "failed" for run_id in run_ids)
    assert server.server_close.call_count == 2


def test_serve_closes_socket_even_if_service_cleanup_fails(monkeypatch):
    server = Mock(server_port=8000)
    server.serve_forever.side_effect = KeyboardInterrupt
    service = Mock()
    service.close.side_effect = RuntimeError("cleanup failed")
    monkeypatch.setattr(web_ui, "create_server", Mock(return_value=server))
    monkeypatch.setattr(web_ui, "BackgroundRuns", Mock(return_value=service))
    with pytest.raises(RuntimeError, match="cleanup failed"):
        web_ui.serve()
    server.server_close.assert_called_once()


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
        assert running["input"] == "[redacted]"
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status, detail = _request(f"{base_url}/api/runs/{launched['id']}")
            if detail["status"] != "running":
                break
            time.sleep(0.01)

        assert status == 200
        assert detail["status"] == "success"
        assert detail["input"] == "[redacted]"
        assert detail["result"] == f"[redacted]: {output}"
        assert database.query_database(
            "SELECT toolName, queryText, resultText FROM ToolResults"
        ) == [("sherlock", "alice", f"alice: {output}")]

        status, recent = _request(f"{base_url}/api/runs?limit=1")
        assert status == 200
        assert recent[0]["id"] == launched["id"]
        assert recent[0]["status"] == "success"

        page_request = Request(
            base_url,
            headers={"Authorization": "Bearer " + "operator-secret"},
        )
        with urlopen(page_request, timeout=2) as response:
            page = response.read().decode()
        assert "Authorization required" in page
        assert 'id="authorized" type="checkbox" required' in page
        assert 'document.querySelector("#detail-output").textContent = run.result' in page
        assert "innerHTML" not in page
    finally:
        server.shutdown()
        server.server_close()


def test_web_run_views_redact_legacy_records_without_rewriting_them(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "web.db"))
    database.setup_database()
    with database.connect_db() as conn:
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
        assert database.query_database(
            "SELECT queryText, errorText FROM ToolRuns"
        ) == [("private@example.com", "private%40example.com")]
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


def test_browser_authentication_roles(tmp_path, monkeypatch):
    runner = Mock(return_value="should not run")
    server, base_url = _server(tmp_path, monkeypatch, runner)
    try:
        status, body = _request(
            base_url,
            headers={"Authorization": "Bearer " + "invalid-secret"},
        )
        assert status == 401
        assert body["error"] == "Invalid credentials."

        page_request = Request(
            base_url,
            headers={"Authorization": "Bearer " + "viewer-secret"},
        )
        with urlopen(page_request, timeout=2) as response:
            page = response.read().decode()
        assert "Authorized tool runs" in page

        status, body = _request(
            f"{base_url}/api/runs",
            {"tool": "sherlock", "input": "alice", "authorized": True},
            {"Authorization": "Bearer " + "viewer-secret"},
        )
        assert status == 403
        assert body["error"] == "Operator role required."

        status, body = _request(
            base_url,
            headers={"Authorization": ""},
        )
        assert status == 401
        assert body["error"] == "Authentication required."
        runner.assert_not_called()
    finally:
        server.shutdown()
        server.server_close()


def test_missing_auth_configuration_and_non_loopback_bind_fail_closed(monkeypatch):
    monkeypatch.delenv("OSINTBEAST_OPERATOR_TOKEN", raising=False)
    monkeypatch.delenv("OSINTBEAST_VIEWER_TOKEN", raising=False)
    monkeypatch.delenv("OSINTBEAST_API_TOKEN", raising=False)
    monkeypatch.delenv("OSINTBEAST_TOKEN", raising=False)
    monkeypatch.delenv("OSINTBEAST_WEB_TOKEN", raising=False)

    try:
        web_ui.serve("127.0.0.1", 8000)
    except RuntimeError as exc:
        assert "tokens" in str(exc)
    else:
        raise AssertionError("serve() started without authentication configured")

    monkeypatch.setenv("OSINTBEAST_OPERATOR_TOKEN", "operator-secret")
    monkeypatch.setenv("OSINTBEAST_VIEWER_TOKEN", "viewer-secret")
    try:
        web_ui.serve("0.0.0.0", 8000)
    except ValueError as exc:
        assert "loopback" in str(exc)
    else:
        raise AssertionError("serve() accepted a non-loopback bind")


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
        assert detail["input"] == "[redacted]"
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
