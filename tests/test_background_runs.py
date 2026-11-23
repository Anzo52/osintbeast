import os
import sqlite3
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


def test_pending_outcome_retries_without_status_requests_after_stale_reconciliation(
    service, monkeypatch
):
    finish = database.finish_tool_run
    monkeypatch.setattr(
        database, "finish_tool_run",
        Mock(side_effect=sqlite3.OperationalError("database unavailable")),
    )
    run_id = service.submit("maigret", "user")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with service._lock:
            if run_id in service._pending:
                break
        time.sleep(0.01)
    else:
        pytest.fail("Run outcome was not queued for retry")

    with database.connect_db() as conn:
        conn.execute(
            "UPDATE ToolRuns SET startedAt = datetime('now', '-11 minutes') "
            "WHERE runID = ?",
            (run_id,),
        )
    assert database.reconcile_stale_tool_runs() == 1
    monkeypatch.setattr(database, "finish_tool_run", finish)

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        run = database.get_tool_run(run_id)
        with service._lock:
            pending = run_id in service._pending
        if run[3] == "success" and not pending:
            break
        time.sleep(0.01)
    else:
        pytest.fail("Pending outcome was not persisted without a status request")

    assert run[4] == "output"
    assert len(database.list_tool_results()) == 1


def test_already_committed_completion_retry_does_not_duplicate_result(
    service, monkeypatch
):
    finish = database.finish_tool_run
    calls = 0

    def commit_then_lose_acknowledgement(*args, **kwargs):
        nonlocal calls
        calls += 1
        finish(*args, **kwargs)
        if calls == 1:
            raise sqlite3.OperationalError("acknowledgement lost")

    monkeypatch.setattr(
        database, "finish_tool_run", commit_then_lose_acknowledgement
    )
    run_id = service.submit("maigret", "user")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with service._lock:
            pending = run_id in service._pending
        if not pending and database.get_tool_run(run_id)[3] == "success":
            break
        time.sleep(0.01)
    else:
        pytest.fail("Committed completion was not safely retried")

    assert calls >= 2
    assert len(database.list_tool_results()) == 1


def test_submission_idempotency_is_scoped_to_key_and_input(service):
    run_id = service.submit("maigret", "user", "submission-1")

    assert service.submit("maigret", " user ", "submission-1") == run_id
    with pytest.raises(background_runs.IdempotencyConflict):
        service.submit("maigret", "other", "submission-1")
    with pytest.raises(background_runs.IdempotencyConflict):
        service.submit("sherlock", "user", "submission-1")
    assert len(database.list_tool_runs()) == 1


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
