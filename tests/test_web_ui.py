import http.client
import json
import shutil
import sqlite3
import socket
import subprocess
import threading
import time
import uuid
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import pytest

import background_runs
import data.utils.database as database
import web_ui
from sherlock_integration import validate_username

OPERATOR_TOKEN = "o" * 40
VIEWER_TOKEN = "v" * 40


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
    web_ui.RUN_SERVICE.close()


def _request(url, payload=None, headers=None):
    headers = {
        "Authorization": "Bearer " + OPERATOR_TOKEN,
        **(headers or {}),
    }
    if payload is None:
        request = Request(url, headers=headers)
    else:
        headers = {
            "Content-Type": "application/json",
            "Origin": f"{urlsplit(url).scheme}://{urlsplit(url).netloc}",
            "Idempotency-Key": uuid.uuid4().hex,
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
    monkeypatch.setenv("OSINTBEAST_OPERATOR_TOKEN", OPERATOR_TOKEN)
    monkeypatch.setenv("OSINTBEAST_VIEWER_TOKEN", VIEWER_TOKEN)
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
    server = Mock(
        server_port=8000,
        server_address=("127.0.0.1", 8000),
        address_family=socket.AF_INET,
    )
    def create_server(*args, **kwargs):
        monkeypatch.setattr(
            web_ui, "RUN_SERVICE", web_ui.BackgroundRuns(web_ui.RUN_SERVICE.limits)
        )
        return server

    monkeypatch.setattr(web_ui, "create_server", Mock(side_effect=create_server))
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
    server = Mock(
        server_port=8000,
        server_address=("127.0.0.1", 8000),
        address_family=socket.AF_INET,
    )

    def create_server(*args, **kwargs):
        monkeypatch.setattr(
            web_ui, "RUN_SERVICE", web_ui.BackgroundRuns(web_ui.RUN_SERVICE.limits)
        )
        return server

    monkeypatch.setattr(web_ui, "create_server", Mock(side_effect=create_server))
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
    server = Mock(
        server_port=8000,
        server_address=("127.0.0.1", 8000),
        address_family=socket.AF_INET,
    )
    server.serve_forever.side_effect = KeyboardInterrupt
    service = Mock()
    service.close.side_effect = [RuntimeError("cleanup failed"), None]

    def create_server(*args, **kwargs):
        monkeypatch.setattr(web_ui, "RUN_SERVICE", service)
        return server

    monkeypatch.setattr(web_ui, "create_server", Mock(side_effect=create_server))
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
        assert launched["input"] == "[redacted]"

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

        page_request = Request(base_url)
        with urlopen(page_request, timeout=2) as response:
            page = response.read().decode()
        assert "Authorization required" in page
        assert 'id="authorized" type="checkbox" required' in page
        assert 'document.querySelector("#detail-output").textContent = run.result' in page
        assert "innerHTML" not in page
    finally:
        server.shutdown()
        server.server_close()


def test_shutdown_marks_active_runs_failed(tmp_path, monkeypatch, run_service):
    server, base_url = _server(tmp_path, monkeypatch, Mock())
    try:
        status, launched = _request(
            f"{base_url}/api/runs",
            {"tool": "sherlock", "input": "slow", "authorized": True},
        )
        assert status == 202
        web_ui.RUN_SERVICE.close()

        run = database.get_tool_run(launched["id"])
        assert run[3] == "failed"
        assert run[5] == "Run cancelled."
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


def test_pending_completion_recovers_after_persistence_failure(
    tmp_path, monkeypatch, caplog
):
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
            f"{base_url}/api/runs",
            headers={"Authorization": "Bearer " + ("x" * 40)},
        )
        assert status == 401
        assert body["error"] == "Invalid credentials."

        status, body = _request(
            f"{base_url}/api/runs",
            headers={
                "Authorization": "Be" + "arer " + "caf" + chr(233)
            },
        )
        assert status == 401
        assert body["error"] == "Invalid credentials."

        page_request = Request(base_url)
        with urlopen(page_request, timeout=2) as response:
            page = response.read().decode()
        assert "Authorized tool runs" in page

        connection = http.client.HTTPConnection(
            "127.0.0.1", server.server_port, timeout=2
        )
        connection.request("GET", "/api/runs")
        response = connection.getresponse()
        assert response.status == 401
        assert response.getheader("WWW-Authenticate") == "Bearer"
        response.read()
        connection.close()

        status, body = _request(
            f"{base_url}/api/runs",
            {"tool": "sherlock", "input": "alice", "authorized": True},
            {"Authorization": "Bearer " + VIEWER_TOKEN},
        )
        assert status == 403
        assert body["error"] == "Operator role required."

        status, body = _request(
            f"{base_url}/api/runs",
            headers={"Authorization": ""},
        )
        assert status == 401
        assert body["error"] == "Authentication required."
        runner.assert_not_called()
        assert database.query_database("SELECT * FROM ToolRuns") == []
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(
    ("operator", "viewer"),
    [
        ("", VIEWER_TOKEN),
        (OPERATOR_TOKEN, ""),
        ("short", VIEWER_TOKEN),
        (OPERATOR_TOKEN, "not-valid!"),
        (OPERATOR_TOKEN, OPERATOR_TOKEN),
    ],
)
def test_invalid_ui_tokens_are_rejected(monkeypatch, operator, viewer):
    monkeypatch.setenv("OSINTBEAST_OPERATOR_TOKEN", operator)
    monkeypatch.setenv("OSINTBEAST_VIEWER_TOKEN", viewer)
    with pytest.raises(RuntimeError):
        web_ui._configured_tokens()


def test_localhost_resolution_must_only_return_loopback(monkeypatch):
    monkeypatch.setattr(
        web_ui.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (web_ui.socket.AF_INET, web_ui.socket.SOCK_STREAM, 6, "", ("203.0.113.1", 0))
        ],
    )
    with pytest.raises(ValueError, match="loopback"):
        web_ui._resolve_loopback_host("localhost")


def test_ipv6_loopback_uses_ipv6_server_socket(monkeypatch):
    class FakeServer:
        def __init__(self, address, handler):
            self.address_family = type(self).address_family
            self.address = address

    monkeypatch.setattr(web_ui, "ThreadingHTTPServer", FakeServer)
    server = web_ui._create_http_server(web_ui.ipaddress.ip_address("::1"), 8000)
    assert server.address_family == web_ui.socket.AF_INET6
    assert server.address == ("::1", 8000)


def test_head_rejection_has_no_body(tmp_path, monkeypatch):
    server, _ = _server(tmp_path, monkeypatch, Mock())
    try:
        connection = http.client.HTTPConnection(
            "127.0.0.1", server.server_port, timeout=2
        )
        connection.request(
            "HEAD",
            "/",
            headers={"Authorization": "Bearer " + OPERATOR_TOKEN},
        )
        response = connection.getresponse()
        assert response.status == 405
        assert response.read() == b""
        connection.close()
    finally:
        server.shutdown()
        server.server_close()


def test_launch_idempotency_returns_existing_run(tmp_path, monkeypatch):
    server, base_url = _server(tmp_path, monkeypatch, Mock())
    key = uuid.uuid4().hex
    payload = {"tool": "sherlock", "input": "alice", "authorized": True}
    try:
        status, first = _request(
            f"{base_url}/api/runs",
            payload,
            {"Idempotency-Key": key},
        )
        assert status == 202
        status, retry = _request(
            f"{base_url}/api/runs",
            payload,
            {"Idempotency-Key": key},
        )
        assert status == 202
        assert retry["id"] == first["id"]
        assert len(database.list_tool_runs()) == 1

        status, body = _request(
            f"{base_url}/api/runs",
            {**payload, "input": "bob"},
            {"Idempotency-Key": key},
        )
        assert status == 409
        assert "different run" in body["error"]
        assert len(database.list_tool_runs()) == 1
    finally:
        server.shutdown()
        server.server_close()


def test_missing_auth_configuration_and_non_loopback_bind_fail_closed(monkeypatch):
    monkeypatch.delenv("OSINTBEAST_OPERATOR_TOKEN", raising=False)
    monkeypatch.delenv("OSINTBEAST_VIEWER_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="tokens"):
        web_ui.create_server("127.0.0.1", 0)

    monkeypatch.setenv("OSINTBEAST_OPERATOR_TOKEN", OPERATOR_TOKEN)
    monkeypatch.setenv("OSINTBEAST_VIEWER_TOKEN", VIEWER_TOKEN)
    with pytest.raises(ValueError, match="loopback"):
        web_ui.create_server("0.0.0.0", 0)


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


def test_submission_idempotency_key_returns_existing_run(tmp_path, monkeypatch):
    server, base_url = _server(tmp_path, monkeypatch, Mock())
    payload = {"tool": "sherlock", "input": "alice", "authorized": True}
    headers = {"Idempotency-Key": "retry-this-submission"}
    try:
        status, first = _request(f"{base_url}/api/runs", payload, headers)
        assert status == 202
        status, repeated = _request(f"{base_url}/api/runs", payload, headers)
        assert status == 202
        assert repeated["id"] == first["id"]
        assert len(database.list_tool_runs()) == 1

        status, body = _request(
            f"{base_url}/api/runs",
            {**payload, "input": "bob"},
            headers,
        )
        assert status == 409
        assert "different run" in body["error"]
        assert len(database.list_tool_runs()) == 1
    finally:
        server.shutdown()
        server.server_close()


def test_serve_creates_a_fresh_run_service_each_time(monkeypatch):
    services = []

    class Service:
        def __init__(self):
            self.closed = False
            services.append(self)

        def close(self):
            self.closed = True

    class Server:
        server_port = 8000
        server_address = ("127.0.0.1", server_port)
        address_family = web_ui.socket.AF_INET

        def __init__(self, _address, _handler):
            pass

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    monkeypatch.setattr(web_ui, "BackgroundRuns", Service)
    monkeypatch.setattr(web_ui, "ThreadingHTTPServer", Server)
    monkeypatch.setattr(database, "setup_database", Mock())
    monkeypatch.setenv("OSINTBEAST_OPERATOR_TOKEN", OPERATOR_TOKEN)
    monkeypatch.setenv("OSINTBEAST_VIEWER_TOKEN", VIEWER_TOKEN)

    web_ui.serve()
    web_ui.serve()

    assert len(services) == 2
    assert services[0] is not services[1]
    assert all(service.closed for service in services)


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
        service = web_ui.RUN_SERVICE
        assert service.cancel(first["id"])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if service.status(first["id"])["status"] != "running":
                break
            time.sleep(0.01)
        status, third = _request(f"{base_url}/api/runs", payload)
        assert status == 202
        assert third["id"] not in (first["id"], second["id"])
    finally:
        server.shutdown()
        server.server_close()


def test_browser_exposes_run_id_when_startup_failure_cannot_be_saved(tmp_path, monkeypatch):
    server, base_url = _server(tmp_path, monkeypatch, Mock())
    run_service = web_ui.RUN_SERVICE
    run_id = database.start_tool_run("sherlock", "alice")
    run_service._pending[run_id] = ("failed", "Could not start background worker.")
    monkeypatch.setattr(
        run_service, "submit", Mock(side_effect=background_runs.StartupPersistenceError(run_id))
    )
    try:
        status, body = _request(
            f"{base_url}/api/runs",
            {"tool": "sherlock", "input": "alice", "authorized": True},
        )
        assert status == 503
        assert body["id"] == run_id
        status, detail = _request(f"{base_url}/api/runs/{run_id}")
        assert status == 200
        assert detail["error"] == "Could not start background worker."
    finally:
        server.shutdown()
        server.server_close()


def test_status_on_another_server_does_not_hide_pending_outcome(tmp_path, monkeypatch):
    first, first_url = _server(tmp_path, monkeypatch, Mock())
    second, second_url = _server(tmp_path, monkeypatch, Mock())
    run_id = database.start_tool_run("sherlock", "alice")
    first.run_service._pending[run_id] = ("success", "output")
    try:
        status, _ = _request(f"{second_url}/api/runs/{run_id}")
        assert status == 503
        status, detail = _request(f"{first_url}/api/runs/{run_id}")
        assert status == 200 and detail["status"] == "success"
        status, detail = _request(f"{second_url}/api/runs/{run_id}")
        assert status == 200 and detail["status"] == "success"
    finally:
        first.run_service.close()
        second.run_service.close()
        first.shutdown()
        first.server_close()
        second.shutdown()
        second.server_close()


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is needed to exercise browser JavaScript")
def test_browser_pending_key_storage_and_input_race():
    script = web_ui.INDEX_HTML.split('    const pendingKeyName =', 1)[1]
    script = 'const pendingKeyName =' + script.split('    loadTools().then', 1)[0]
    harness = """
const {webcrypto} = require("node:crypto");
let hook = null;
const crypto = {
  randomUUID: () => webcrypto.randomUUID(),
  subtle: {digest: (...args) => {
    if (hook) { const run = hook; hook = null; run(); }
    return webcrypto.subtle.digest(...args);
  }}
};
const store = new Map();
const sessionStorage = {
  getItem: key => store.get(key) || null,
  setItem: (key, value) => store.set(key, value),
  removeItem: key => store.delete(key)
};
function element(value) {
  return {value, checked: true, handlers: {}, addEventListener(type, cb) { this.handlers[type] = cb; }};
}
const toolSelect = element("holehe"), queryInput = element("person@example.com");
const launch = element(""), message = element(""), submit = element("");
const authorized = element(""), error = element("");
const document = {querySelector: selector => ({
  "#launch": launch, "#message": message, "#submit": submit, "#authorized": authorized
})[selector]};
const updateInputLabel = () => {};
const showError = () => {};
let requests = [], fail = true;
const request = async (url, options) => {
  requests.push(options);
  if (fail) throw new Error("lost acknowledgement");
  return {id: 1, status: "running"};
};
const loadRun = async () => { throw new Error("poll failed"); };
""" + script + """
(async () => {
  const fire = () => launch.handlers.submit({preventDefault() {}});
  await fire();
  const saved = sessionStorage.getItem(pendingKeyName);
  const record = JSON.parse(saved);
  hook = () => { queryInput.value = "edited@example.com"; queryInput.handlers.input(); };
  await fire();
  const clearedOnEdit = sessionStorage.getItem(pendingKeyName) === null;
  const sameKey = requests[0].headers["Idempotency-Key"] === requests[1].headers["Idempotency-Key"];
  const secondBody = JSON.parse(requests[1].body);
  fail = false;
  await fire();
  console.log(JSON.stringify({record, saved, clearedOnEdit, sameKey, secondBody,
    clearedOnConfirmation: sessionStorage.getItem(pendingKeyName) === null}));
})().catch(err => { console.error(err); process.exit(1); });
"""
    result = subprocess.run(
        ["node", "-e", harness], check=True, capture_output=True, text=True, timeout=10,
    )
    outcome = json.loads(result.stdout)
    assert set(outcome["record"]) == {"key", "tool", "fingerprint"}
    assert "person@example.com" not in outcome["saved"]
    assert outcome["record"]["tool"] == "holehe"
    assert outcome["clearedOnEdit"] and outcome["sameKey"]
    assert outcome["secondBody"]["input"] == "person@example.com"
    assert outcome["clearedOnConfirmation"]
