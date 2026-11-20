import json
import threading
import time
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from unittest.mock import Mock

import backend_services
import data.utils.database as database
import web_ui
from sherlock_integration import validate_username


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

        with urlopen(base_url, timeout=2) as response:
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


def test_browser_runs_are_bounded_and_capacity_recovers(tmp_path, monkeypatch):
    release = threading.Event()
    both_started = threading.Event()
    lock = threading.Lock()
    started = []

    def runner(username):
        with lock:
            started.append(username)
            if len(started) == 2:
                both_started.set()
        assert release.wait(5)
        return "output"

    monkeypatch.setattr(web_ui, "RUN_SERVICE", backend_services.BackgroundRunService(2))
    server, base_url = _server(tmp_path, monkeypatch, runner)
    payload = {"tool": "sherlock", "input": "alice", "authorized": True}
    try:
        first_status, first = _request(f"{base_url}/api/runs", payload)
        second_status, second = _request(f"{base_url}/api/runs", payload)
        assert first_status == second_status == 202
        assert first["id"] != second["id"]
        assert both_started.wait(2)

        status, body = _request(f"{base_url}/api/runs", payload)
        assert status == 503
        assert "Too many runs" in body["error"]
        assert len(database.list_tool_runs()) == 2
        assert len(started) == 2

        release.set()
        deadline = time.monotonic() + 2
        while True:
            status, third = _request(f"{base_url}/api/runs", payload)
            if status == 202 or time.monotonic() >= deadline:
                break
            assert status == 503
            time.sleep(0.01)
        assert status == 202
        assert third["id"] not in (first["id"], second["id"])
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if all(row[3] != "running" for row in database.list_tool_runs()):
                break
            time.sleep(0.01)
        assert all(row[3] == "success" for row in database.list_tool_runs())
    finally:
        release.set()
        server.shutdown()
        server.server_close()
