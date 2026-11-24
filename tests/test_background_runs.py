import multiprocessing
import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import background_runs
import data.utils.database as database


def mocked_worker(connection, tool_name, query, max_output_bytes, deadline=None):
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


def blocked_runner(query):
    time.sleep(10)
    return query


def guarded_worker(connection, deadline, ready=None):
    def runner(query):
        if ready is not None:
            ready.set()
        return blocked_runner(query)

    background_runs._TOOLS["maigret"] = (lambda value: value, runner)
    background_runs._worker(connection, "maigret", "user", 100, deadline)


def parent_that_exits(sender, ready):
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=guarded_worker, args=(sender, time.monotonic() + 10, ready)
    )
    process.start()
    if not ready.wait(5):
        os._exit(2)
    os._exit(0)


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
    expected_text = text.replace(query, "[redacted]")
    assert run["resultText" if status == "success" else "errorText"] == expected_text
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


@pytest.mark.parametrize("failure_point", ["constructor", "start"])
def test_thread_start_and_persistence_failure_is_retried(service, monkeypatch, failure_point):
    finish = database.finish_tool_run
    monkeypatch.setattr(database, "finish_tool_run", Mock(side_effect=OSError("unavailable")))
    if failure_point == "constructor":
        monkeypatch.setattr(background_runs.threading, "Thread", Mock(side_effect=RuntimeError("start")))
    else:
        monkeypatch.setattr(background_runs.threading.Thread, "start", Mock(side_effect=RuntimeError("start")))
    with pytest.raises(RuntimeError, match="start"):
        service.submit("maigret", "user")
    run_id = database.list_tool_runs()[0][0]
    with pytest.raises(RuntimeError, match="Could not persist"):
        service.status(run_id)
    monkeypatch.setattr(database, "finish_tool_run", finish)
    run = service.status(run_id)
    assert (run["status"], run["errorText"]) == ("failed", "Could not start background worker.")


def test_received_result_wins_over_late_cancellation_and_timeout(service, monkeypatch):
    run_id = database.start_tool_run("maigret", "user")
    cancel, completed = threading.Event(), threading.Event()
    service._jobs[run_id] = (cancel, Mock(), completed)
    receiver = Mock()
    receiver.poll.return_value = True
    receiver.recv_bytes.return_value = b"Soutput"
    context = Mock()
    context.Pipe.return_value = (receiver, Mock())
    monkeypatch.setattr(service, "_context", context)
    monkeypatch.setattr(background_runs, "time", SimpleNamespace(monotonic=Mock(side_effect=[10, 12])))
    late_cancellation = []
    monkeypatch.setattr(background_runs, "_stop_process", lambda process: late_cancellation.append(
        service.cancel(run_id)
    ))
    service._supervise(run_id, "maigret", "user", cancel, completed)
    assert service.status(run_id)["status"] == "success"
    assert late_cancellation == [False]
    receiver.poll.assert_called_once_with(0)


def test_worker_enforces_its_own_deadline():
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=guarded_worker, args=(sender, time.monotonic() + 1))
    try:
        process.start()
        sender.close()
        process.join(5)
        assert not process.is_alive()
        assert process.exitcode != 0
    finally:
        if process.is_alive():
            process.kill()
            process.join()
        receiver.close()
        sender.close()


def test_worker_exits_when_parent_dies():
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    ready = context.Event()
    parent = context.Process(target=parent_that_exits, args=(sender, ready))
    try:
        parent.start()
        sender.close()
        parent.join(5)
        assert parent.exitcode == 0
        assert receiver.poll(3)
        with pytest.raises(EOFError):
            receiver.recv_bytes()
    finally:
        if parent.is_alive():
            parent.kill()
            parent.join()
        receiver.close()
        sender.close()


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
    payload = connection.send_bytes.call_args.args[0]
    assert payload.startswith(prefix)
    assert len(payload) <= limit + 1
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


def test_holehe_worker_starts_watchdog_and_executes_once(monkeypatch):
    sensitive_value = "person@example.com"
    runner = Mock(return_value={"output": f"Found {sensitive_value}"})
    watchdog = Mock()
    parent = SimpleNamespace(sentinel=object())
    if os.name == "posix":
        monkeypatch.setattr(os, "setsid", lambda: None)
    monkeypatch.setattr(background_runs.multiprocessing, "parent_process", lambda: parent)
    monkeypatch.setattr(background_runs.threading, "Thread", Mock(return_value=watchdog))
    monkeypatch.setitem(background_runs._TOOLS, "holehe", (lambda value: value, runner))
    connection = Mock()

    background_runs._worker(connection, "holehe", sensitive_value, 1024, deadline=123)

    watchdog.start.assert_called_once_with()
    runner.assert_called_once_with(sensitive_value)
    connection.send_bytes.assert_called_once_with(b"SFound [redacted]")


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
