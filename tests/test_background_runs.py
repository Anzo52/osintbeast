import os
import threading
import time
from unittest.mock import Mock

import pytest

import background_runs
import data.utils.database as database


def mocked_worker(connection, tool_name, query, max_output_bytes):
    if query == "crash":
        os._exit(1)
    if query == "slow":
        time.sleep(10)
    if query == "large":
        connection.send_bytes(b"S" + b"x" * (max_output_bytes + 1))
    elif query == "error":
        connection.send_bytes(b"Fmock failure")
    elif query == "timeout":
        connection.send_bytes(b"Tmock timeout")
    else:
        connection.send_bytes(b"Soutput")
    connection.close()


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "runs.db"))
    database.setup_database()
    monkeypatch.setattr(background_runs, "_worker", mocked_worker)
    service = background_runs.BackgroundRuns(
        background_runs.RunLimits(max_concurrent=1, timeout_seconds=1, max_output_bytes=100)
    )
    yield service
    service.close()


def wait_for_run(service, run_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        run = service.status(run_id)
        if run["status"] != "running":
            return run
        time.sleep(0.01)
    pytest.fail("Run did not reach a terminal state")


@pytest.mark.parametrize(
    ("query", "status", "text"),
    [
        ("user", "success", "output"),
        ("error", "failed", "mock failure"),
        ("timeout", "timed_out", "mock timeout"),
        ("crash", "failed", "Background worker failed."),
        ("large", "failed", "Background worker failed."),
        ("slow", "timed_out", "Background run exceeded its time limit."),
    ],
)
def test_background_outcomes_are_persisted(service, query, status, text):
    run_id = service.submit("maigret", query)
    run = wait_for_run(service, run_id)
    assert run["status"] == status
    assert run["resultText" if status == "success" else "errorText"] == text
    assert run["finishedAt"] is not None
    assert len(database.list_tool_results()) == (1 if status == "success" else 0)


def test_background_runs_redact_sensitive_input_and_output(service):
    sensitive_value = "person@example.com"
    run = wait_for_run(service, service.submit("holehe", sensitive_value))

    assert run["queryText"] == "[redacted]"
    assert run["resultText"] == "output"
    assert database.list_tool_results()[0][2:4] == (
        "[redacted]", "output"
    )


def test_capacity_rejection_and_cancellation(service):
    start = time.monotonic()
    run_id = service.submit("maigret", "slow")
    assert time.monotonic() - start < 0.5
    assert service.status(run_id)["status"] == "running"
    with pytest.raises(background_runs.CapacityError):
        service.submit("maigret", "another")
    assert service.cancel(run_id)
    run = wait_for_run(service, run_id)
    assert (run["status"], run["errorText"]) == ("failed", "Run cancelled.")
    assert not service.cancel(run_id)
    assert len(database.query_database("SELECT runID FROM ToolRuns")) == 1
    assert wait_for_run(service, service.submit("maigret", "user"))["status"] == "success"


def test_invalid_input_and_unknown_status(service):
    for tool, query in [("unknown", "user"), ("maigret", "x" * 255), ("maigret", "bad user")]:
        with pytest.raises(ValueError):
            service.submit(tool, query)
    assert service.status(123) is None
    assert database.query_database("SELECT runID FROM ToolRuns") == []


def test_close_cancels_active_work_and_rejects_new_work(service):
    run_id = service.submit("maigret", "slow")
    service.close()
    assert service.status(run_id)["errorText"] == "Run cancelled."
    with pytest.raises(RuntimeError, match="closed"):
        service.submit("maigret", "user")


def test_process_start_failure_is_recorded(service, monkeypatch):
    process = Mock(pid=None)
    process.start.side_effect = OSError("cannot start")
    monkeypatch.setattr(service._context, "Process", lambda **kwargs: process)
    run = wait_for_run(service, service.submit("maigret", "user"))
    assert (run["status"], run["errorText"]) == ("failed", "Background worker failed.")


def test_thread_start_failure_releases_capacity(service, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(background_runs.threading, "Thread", Mock(side_effect=RuntimeError("unavailable")))
        with pytest.raises(RuntimeError):
            service.submit("maigret", "user")
    assert wait_for_run(service, service.submit("maigret", "user"))["status"] == "success"


def test_database_start_failure_releases_capacity(service, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(background_runs, "start_tool_run", Mock(side_effect=OSError("unavailable")))
        with pytest.raises(OSError):
            service.submit("maigret", "user")
    assert wait_for_run(service, service.submit("maigret", "user"))["status"] == "success"


def test_persistence_failure_is_reported_and_retried(service, monkeypatch):
    finish = database.finish_tool_run
    monkeypatch.setattr(database, "finish_tool_run", Mock(side_effect=OSError("database unavailable")))
    run_id = service.submit("maigret", "user")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            service.status(run_id)
        except RuntimeError as exc:
            assert "Could not persist" in str(exc)
            break
        time.sleep(0.01)
    else:
        pytest.fail("Persistence failure was not reported")
    with pytest.raises(background_runs.CapacityError):
        service.submit("maigret", "another")
    monkeypatch.setattr(database, "finish_tool_run", finish)
    assert service.status(run_id)["status"] == "success"
    assert len(database.list_tool_results()) == 1
    assert wait_for_run(service, service.submit("maigret", "another"))["status"] == "success"


def test_thread_start_and_persistence_failure_exposes_run_id(service, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(background_runs.threading, "Thread", Mock(side_effect=RuntimeError("unavailable")))
        patch.setattr(database, "finish_tool_run", Mock(side_effect=OSError("unavailable")))
        with pytest.raises(background_runs.StartupPersistenceError) as error:
            service.submit("maigret", "user")
        run_id = error.value.run_id
        assert database.get_tool_run(run_id)[3] == "running"
        with pytest.raises(background_runs.CapacityError):
            service.submit("maigret", "another")
    assert service.status(run_id)["errorText"] == "Could not start background worker."
    assert wait_for_run(service, service.submit("maigret", "another"))["status"] == "success"


def test_reconciled_pending_failure_releases_capacity(service, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(background_runs.threading, "Thread", Mock(side_effect=RuntimeError("unavailable")))
        patch.setattr(database, "finish_tool_run", Mock(side_effect=OSError("unavailable")))
        with pytest.raises(background_runs.StartupPersistenceError) as error:
            service.submit("maigret", "user")
    run_id = error.value.run_id
    with database.connect_db() as connection:
        connection.execute(
            "UPDATE ToolRuns SET startedAt = '2000-01-01 00:00:00' WHERE runID = ?",
            (run_id,),
        )
    assert database.reconcile_stale_tool_runs(1) == 1
    assert service.status(run_id)["errorText"] == "Run outcome could not be persisted."
    assert wait_for_run(service, service.submit("maigret", "another"))["status"] == "success"


@pytest.mark.parametrize("outcome", ["result", "timeout", "crash"])
def test_terminal_outcomes_survive_late_cancellation(service, monkeypatch, outcome):
    run_id = database.start_tool_run("maigret", "user")
    cancel, completed = threading.Event(), threading.Event()
    service._jobs[run_id] = (cancel, Mock(), completed)
    receiver, sender, process = Mock(), Mock(), Mock()
    process.pid = 1
    process.is_alive.return_value = outcome != "crash"
    receiver.poll.return_value = outcome in ("result", "crash")
    if outcome == "result":
        def receive(*args):
            assert service.cancel(run_id)
            return b"Soutput"
        receiver.recv_bytes.side_effect = receive
    elif outcome == "crash":
        receiver.recv_bytes.side_effect = EOFError()
    monkeypatch.setattr(service._context, "Pipe", lambda **kwargs: (receiver, sender))
    monkeypatch.setattr(service._context, "Process", lambda **kwargs: process)
    monkeypatch.setattr(background_runs.time, "monotonic", Mock(side_effect=[0, 2]))

    def stop(_):
        if outcome == "timeout":
            assert not service.cancel(run_id)

    monkeypatch.setattr(background_runs, "_stop_process", stop)
    service._supervise(run_id, "maigret", "user", cancel, completed)
    run = service.status(run_id)
    assert run["status"] == {"result": "success", "timeout": "timed_out", "crash": "failed"}[outcome]
    assert run["resultText" if outcome == "result" else "errorText"] == {
        "result": "output",
        "timeout": "Background run exceeded its time limit.",
        "crash": "Background worker failed.",
    }[outcome]


@pytest.mark.parametrize(
    ("result", "limit", "prefix"),
    [
        ("output", 100, b"Soutput"),
        ("too long", 3, b"FT"),
        (TimeoutError("timeout"), 100, b"Ttimeout"),
        (RuntimeError("failure"), 100, b"Ffailure"),
    ],
)
def test_worker_wraps_mocked_execution(monkeypatch, result, limit, prefix):
    if os.name == "posix":
        monkeypatch.setattr(os, "setsid", lambda: None)
    runner = Mock(side_effect=result) if isinstance(result, Exception) else Mock(return_value=result)
    monkeypatch.setitem(background_runs._TOOLS, "maigret", (lambda value: value, runner))
    connection = Mock()
    background_runs._worker(connection, "maigret", "user", limit)
    assert connection.send_bytes.call_args.args[0].startswith(prefix)
    runner.assert_called_once_with("user")
    connection.close.assert_called_once()


def test_worker_sanitizes_unexpected_exception(monkeypatch):
    if os.name == "posix":
        monkeypatch.setattr(os, "setsid", lambda: None)
    monkeypatch.setitem(
        background_runs._TOOLS, "maigret",
        (lambda value: value, Mock(side_effect=Exception("private exception"))),
    )
    connection = Mock()
    background_runs._worker(connection, "maigret", "user", 1024)
    assert connection.send_bytes.call_args.args[0] == b"Fmaigret failed unexpectedly (Exception)."


def test_worker_redacts_sensitive_output_and_suppresses_console_output(
    monkeypatch, capsys
):
    sensitive_value = "person@example.com"

    def run(query):
        print(f"Starting lookup for {query}")
        return f"Found {query}"

    if os.name == "posix":
        monkeypatch.setattr(os, "setsid", lambda: None)
    monkeypatch.setitem(
        background_runs._TOOLS,
        "holehe",
        (lambda value: value, lambda value: {"output": run(value)}),
    )
    connection = Mock()

    background_runs._worker(
        connection, "holehe", sensitive_value, 1024
    )

    payload = connection.send_bytes.call_args.args[0]
    captured = capsys.readouterr()
    assert payload == b"SFound [redacted]"
    assert sensitive_value not in captured.out
    assert sensitive_value not in captured.err
    connection.close.assert_called_once()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_concurrent": 0}, {"max_query_chars": -1},
        {"max_output_bytes": 1.5}, {"timeout_seconds": 0},
        {"timeout_seconds": float("inf")}, {"timeout_seconds": float("nan")},
    ],
)
def test_invalid_limits(kwargs):
    with pytest.raises(ValueError):
        background_runs.RunLimits(**kwargs)
