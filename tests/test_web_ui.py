import json
import threading
import time
from http.server import ThreadingHTTPServer
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import web_ui
from data.utils import database
from sherlock_integration import validate_username


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
    runner_started = threading.Event()
    release_runner = threading.Event()

    def runner(username):
        runner_started.set()
        assert release_runner.wait(2)
        return f"{username}: {output}"

    server, base_url = _server(tmp_path, monkeypatch, runner)
    try:
        status, launched = _request(
            f"{base_url}/api/runs",
            {"tool": "sherlock", "input": " alice ", "authorized": True},
        )
        assert status == 202
        assert launched["status"] == "running"
        assert launched["input"] == "alice"

        assert runner_started.wait(1)
        status, running = _request(f"{base_url}/api/runs/{launched['id']}")
        assert status == 200
        assert running["status"] == "running"
        release_runner.set()

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status, detail = _request(f"{base_url}/api/runs/{launched['id']}")
            if detail["status"] != "running":
                break
            time.sleep(0.01)

        assert status == 200
        assert detail["status"] == "success"
        assert detail["result"] == f"alice: {output}"
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
        release_runner.set()
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
            {"tool": "sherlock", "input": "alice", "authorized": True},
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
