import json
import multiprocessing
import os
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import background_runs
from data.utils import database


def mocked_worker(
    connection, tool_name, query, max_output_bytes, deadline=None,
    timed_out=None, spool_context=None,
):
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


def unredacted_sensitive_runner(query):
    text = f"lookup result for {query}"
    if query.startswith("failure@"):
        raise ValueError(text)
    return {"output": text}


def sensitive_worker(
    connection, tool_name, query, max_output_bytes, deadline=None,
    timed_out=None, spool_context=None,
):
    background_runs._TOOLS["holehe"] = (lambda value: value, unredacted_sensitive_runner)
    background_runs._worker(
        connection, tool_name, query, max_output_bytes, deadline, timed_out, spool_context
    )


def durable_worker(
    connection, tool_name, query, max_output_bytes, deadline=None,
    timed_out=None, spool_context=None,
):
    background_runs._write_spooled_outcome(
        *spool_context, "success", "durable output"
    )
    connection.send_bytes(b"Sdurable output")
    connection.close()


def sleeping_worker(
    connection, tool_name, query, max_output_bytes, deadline=None,
    timed_out=None, spool_context=None,
):
    time.sleep(0.5)
    background_runs._write_spooled_outcome(
        *spool_context, "success", "renewed output"
    )
    connection.send_bytes(b"Srenewed output")
    connection.close()


def blocked_worker(
    connection, tool_name, query, max_output_bytes, deadline=None,
    timed_out=None, spool_context=None,
):
    time.sleep(30)


def cancellation_race_worker(
    connection, tool_name, query, max_output_bytes, deadline=None,
    timed_out=None, spool_context=None,
):
    directory = Path(spool_context[0])
    (directory / "worker-ready").touch()
    while not (directory / "write-outcome").exists():
        time.sleep(0.01)
    background_runs._write_spooled_outcome(
        *spool_context, "success", "late worker outcome"
    )
    (directory / "outcome-written").touch()
    time.sleep(30)


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


@pytest.mark.parametrize(
    ("prefix", "status", "field"),
    [(b"S", "success", "resultText"), (b"F", "failed", "errorText")],
)
def test_background_outcomes_redact_sensitive_query(
    service, monkeypatch, prefix, status, field
):
    sensitive_value = "person@example.com"

    query = sensitive_value if prefix == b"S" else "failure@example.com"
    monkeypatch.setattr(
        background_runs, "_worker", sensitive_worker
    )
    run_id = service.submit("holehe", query)
    run = wait_for_run(service, run_id)

    assert run["status"] == status
    assert run[field] == "lookup result for [redacted]"
    assert database.query_database(
        "SELECT queryText, resultText, errorText FROM ToolRuns WHERE runID = ?",
        (run_id,),
    ) == [(
        "[redacted]",
        "lookup result for [redacted]" if status == "success" else None,
        "lookup result for [redacted]" if status == "failed" else None,
    )]


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


@pytest.mark.parametrize("shutdown", [False, True])
@pytest.mark.parametrize("spool_during_cancel", [False, True])
def test_active_cancellation_reconciles_supervisor_and_worker_spools(
    service, monkeypatch, shutdown, spool_during_cancel
):
    monkeypatch.setattr(background_runs, "_worker", cancellation_race_worker)
    service.limits = background_runs.RunLimits(
        max_concurrent=1, timeout_seconds=15, max_output_bytes=100
    )
    original_cancel = database.cancel_background_run

    def wait_for_file(path):
        deadline = time.monotonic() + 8
        while not path.exists():
            if time.monotonic() >= deadline:
                pytest.fail(f"Timed out waiting for {path.name}")
            time.sleep(0.01)

    def cancel_with_worker_race(run_id):
        cancelled = original_cancel(run_id)
        if cancelled and spool_during_cancel:
            (service._spool_dir / "write-outcome").touch()
            wait_for_file(service._spool_dir / "outcome-written")
        return cancelled

    original_stop = background_runs._stop_process

    def stop_after_worker_race(process):
        if spool_during_cancel:
            wait_for_file(service._spool_dir / "outcome-written")
        original_stop(process)

    monkeypatch.setattr(database, "cancel_background_run", cancel_with_worker_race)
    monkeypatch.setattr(background_runs, "_stop_process", stop_after_worker_race)
    run_id = service.submit("maigret", "user")
    wait_for_file(service._spool_dir / "worker-ready")
    with service._lock:
        supervisor = service._jobs[run_id][1]
    if shutdown:
        service.close()
    else:
        assert service.cancel(run_id)
    supervisor.join(8)
    assert not supervisor.is_alive()
    assert database.get_background_run_state(run_id)[0] == "cancelled"
    assert database.get_tool_run(run_id)[3:6] == ("failed", None, "Run cancelled.")
    assert database.list_tool_results() == []
    assert list(service._spool_dir.glob(f"run-{run_id}-*.json")) == []


def test_close_is_bounded_when_final_persistence_stalls(service, monkeypatch, caplog):
    entered, release = threading.Event(), threading.Event()
    finish = database.finish_background_run

    def stalled_finish(*args, **kwargs):
        entered.set()
        release.wait(5)
        return finish(*args, **kwargs)

    monkeypatch.setattr(database, "finish_background_run", stalled_finish)
    monkeypatch.setattr(background_runs, "_SHUTDOWN_TIMEOUT_SECONDS", 0.1)
    service.submit("maigret", "user")
    try:
        assert entered.wait(5)
        start = time.monotonic()
        service.close()
        assert time.monotonic() - start < 0.5
        assert "shutdown wait exceeded" in caplog.text
    finally:
        release.set()
        monkeypatch.setattr(background_runs, "_SHUTDOWN_TIMEOUT_SECONDS", 5)
        service.close()


def test_persisting_job_uses_only_one_capacity_slot(service, monkeypatch):
    service.limits = background_runs.RunLimits(max_concurrent=2)
    entered, release = threading.Event(), threading.Event()
    finish = database.finish_background_run

    def stalled_finish(run_id, *args, **kwargs):
        if run_id == first:
            entered.set()
            assert release.wait(5)
        return finish(run_id, *args, **kwargs)

    first = service.submit("maigret", "slow")
    monkeypatch.setattr(database, "finish_background_run", stalled_finish)
    try:
        assert service.cancel(first)
        assert entered.wait(5)
        assert not service.cancel(first)
        second = service.submit("maigret", "user")
        assert second != first
    finally:
        release.set()
    assert wait_for_run(service, second)["status"] == "success"


def test_process_start_failure_is_recorded(service, monkeypatch):
    process = Mock(pid=None)
    process.start.side_effect = OSError("cannot start")
    monkeypatch.setattr(service._context, "Process", lambda **kwargs: process)
    run = wait_for_run(service, service.submit("maigret", "user"))
    assert (run["status"], run["errorText"]) == ("failed", "Background worker failed.")


@pytest.mark.parametrize("failure_point", ["constructor", "start"])
def test_thread_start_and_persistence_failure_is_retried(service, monkeypatch, failure_point):
    monkeypatch.setattr(
        database, "finish_background_run",
        Mock(side_effect=OSError("unavailable")),
    )
    if failure_point == "constructor":
        monkeypatch.setattr(background_runs.threading, "Thread", Mock(side_effect=RuntimeError("start")))
    else:
        monkeypatch.setattr(background_runs.threading.Thread, "start", Mock(side_effect=RuntimeError("start")))
    with pytest.raises(RuntimeError, match="start"):
        service.submit("maigret", "user")
    run_id = database.list_tool_runs()[0][0]
    assert service.status(run_id)["status"] == "running"


@pytest.mark.parametrize(
    ("outcome", "result_text", "error_text"),
    [
        ("success", "late result", None),
        ("failed", None, "late failure"),
    ],
)
def test_terminal_state_cancellation_cannot_be_overwritten(
    service, outcome, result_text, error_text
):
    owner = "d" * 32
    run_id, created, attempt = database.create_background_run(
        "maigret", "user", owner_token=owner,
        lease_expires_at=background_runs._now_ms() + 60_000,
    )
    assert created and attempt == 1
    assert database.cancel_background_run(run_id)
    assert not database.finish_background_run(
        run_id, owner, attempt, outcome,
        result_text=result_text, error_text=error_text,
    )
    assert database.get_tool_run(run_id)[3:6] == (
        "failed", None, "Run cancelled."
    )
    assert database.list_tool_results() == []


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
        patch.setattr(database, "create_background_run", Mock(side_effect=OSError("unavailable")))
        with pytest.raises(OSError):
            service.submit("maigret", "user")
    assert wait_for_run(service, service.submit("maigret", "user"))["status"] == "success"


def test_persistence_failure_is_reported_and_retried(service, monkeypatch):
    finish = database.finish_background_run
    monkeypatch.setattr(
        database, "finish_background_run",
        Mock(side_effect=OSError("database unavailable")),
    )
    run_id = service.submit("maigret", "user")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not list(
        service._spool_dir.glob(f"run-{run_id}-*.json")
    ):
        time.sleep(0.01)
    assert service.status(run_id)["status"] == "running"
    monkeypatch.setattr(database, "finish_background_run", finish)
    assert wait_for_run(service, run_id)["status"] == "success"
    assert len(database.list_tool_results()) == 1
    assert wait_for_run(service, service.submit("maigret", "another"))["status"] == "success"


def test_thread_start_and_persistence_failure_exposes_run_id(service, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(background_runs.threading, "Thread", Mock(side_effect=RuntimeError("unavailable")))
        patch.setattr(database, "finish_background_run", Mock(side_effect=OSError("unavailable")))
        with pytest.raises(background_runs.StartupPersistenceError) as error:
            service.submit("maigret", "user", "startup-retry")
        run_id = error.value.run_id
        assert service.submit("maigret", "user", "startup-retry") == run_id
        with pytest.raises(background_runs.IdempotencyConflict):
            service.submit("maigret", "another", "startup-retry")
        assert database.get_tool_run(run_id)[3] == "running"
    assert service.status(run_id)["errorText"] == "Could not start background worker."
    assert wait_for_run(service, service.submit("maigret", "another"))["status"] == "success"


def test_cancellation_during_poll_is_not_replaced_by_a_later_result(service, monkeypatch):
    run_id = database.start_tool_run("maigret", "user")
    database.ensure_background_run(run_id)
    cancel, completed = threading.Event(), threading.Event()
    service._jobs[run_id] = (cancel, Mock(), completed)
    receiver, sender, process = Mock(), Mock(), Mock()
    process.pid = 1

    def poll(_):
        assert service.cancel(run_id)
        return True

    receiver.poll.side_effect = poll
    receiver.recv_bytes.return_value = b"Soutput"
    monkeypatch.setattr(service._context, "Pipe", lambda **kwargs: (receiver, sender))
    monkeypatch.setattr(service._context, "Process", lambda **kwargs: process)
    monkeypatch.setattr(background_runs, "_stop_process", lambda _: None)
    service._supervise(run_id, "maigret", "user", cancel, completed)
    assert service.status(run_id)["errorText"] == "Run cancelled."
    receiver.recv_bytes.assert_not_called()


def test_persistence_failure_retries_durable_outcome_without_status(service, monkeypatch):
    finish = database.finish_background_run
    attempts = 0

    def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("database unavailable")
        return finish(*args, **kwargs)

    monkeypatch.setattr(database, "finish_background_run", fail_once)
    run_id = service.submit("maigret", "user")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        run = database.get_tool_run(run_id)
        if run[3] == "success":
            break
        time.sleep(0.02)
    else:
        pytest.fail("Durable outcome was not persisted by the retry worker")

    assert database.get_tool_run(run_id)[4] == "output"
    assert attempts >= 2
    assert list(service._spool_dir.glob(f"run-{run_id}-*.json")) == []
    assert len(database.list_tool_results()) == 1


def test_repeated_persistence_failures_use_increasing_retry_backoff(
    service, monkeypatch
):
    finish = database.finish_background_run
    attempts = []

    def fail_three_times(*args, **kwargs):
        attempts.append(time.monotonic())
        if len(attempts) <= 3:
            raise sqlite3.OperationalError("database unavailable")
        return finish(*args, **kwargs)

    monkeypatch.setattr(database, "finish_background_run", fail_three_times)
    run_id = service.submit("maigret", "user")
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and database.get_tool_run(run_id)[3] != "success":
        time.sleep(0.02)

    assert database.get_tool_run(run_id)[3] == "success"
    assert len(attempts) >= 4
    assert attempts[2] - attempts[1] >= 0.4
    assert attempts[3] - attempts[2] >= 0.8


def test_already_committed_completion_retry_does_not_duplicate_result(
    service, monkeypatch
):
    finish = database.finish_background_run
    calls = 0

    def commit_then_lose_acknowledgement(*args, **kwargs):
        nonlocal calls
        calls += 1
        finish(*args, **kwargs)
        if calls == 1:
            raise sqlite3.OperationalError("acknowledgement lost")

    monkeypatch.setattr(database, "finish_background_run", commit_then_lose_acknowledgement)
    run_id = service.submit("maigret", "user")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if database.get_tool_run(run_id)[3] == "success" and not list(
            service._spool_dir.glob(f"run-{run_id}-*.json")
        ):
            break
        time.sleep(0.01)
    else:
        pytest.fail("Committed completion was not safely retried")

    assert calls >= 2
    assert len(database.list_tool_results()) == 1


def test_submission_idempotency_is_scoped_to_key_and_input(service):
    dispatch = Mock(wraps=service._supervise)
    service._supervise = dispatch
    run_id = service.submit("maigret", "user", "submission-1")

    assert service.submit("maigret", " user ", "submission-1") == run_id
    assert dispatch.call_count == 1
    with pytest.raises(background_runs.IdempotencyConflict):
        service.submit("maigret", "other", "submission-1")
    with pytest.raises(background_runs.IdempotencyConflict):
        service.submit("sherlock", "user", "submission-1")
    assert len(database.list_tool_runs()) == 1


def test_simultaneous_duplicate_submissions_share_one_run_across_instances(service):
    second = background_runs.BackgroundRuns(service.limits)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda instance: instance.submit(
                    "maigret", "same-user", "concurrent-key"
                ),
                (service, second),
            ))
        assert results[0] == results[1]
        assert len(database.list_tool_runs()) == 1
        assert wait_for_run(service, results[0])["status"] == "success"
        assert len(database.list_tool_results()) == 1
    finally:
        second.close()


def test_failed_start_retries_persistence_and_keeps_idempotency_key(service, monkeypatch):
    finish = database.finish_background_run
    attempts = 0

    def finish_after_failure(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("temporarily unavailable")
        return finish(*args, **kwargs)

    monkeypatch.setattr(database, "finish_background_run", finish_after_failure)
    thread_class = threading.Thread

    def thread_factory(*args, **kwargs):
        if kwargs.get("name", "").startswith("tool-run-") and kwargs["name"] != "tool-run-persistence-retry":
            raise RuntimeError("worker thread unavailable")
        return thread_class(*args, **kwargs)

    monkeypatch.setattr(background_runs.threading, "Thread", thread_factory)
    with pytest.raises(RuntimeError, match="worker thread unavailable"):
        service.submit("maigret", "user", "failed-start-key")
    run_id = database.list_tool_runs()[0][0]
    assert service.submit("maigret", "user", "failed-start-key") == run_id
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and database.get_tool_run(run_id)[3] != "failed":
        time.sleep(0.01)
    assert database.get_tool_run(run_id)[3] == "failed"
    assert list(service._spool_dir.glob(f"run-{run_id}-*.json")) == []
    assert len(database.list_tool_runs()) == 1


def test_persistence_does_not_hold_service_lock_or_consume_capacity(service, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    finish = database.finish_background_run
    first = None

    def slow_write(*args, **kwargs):
        if args[0] == first:
            started.set()
            assert release.wait(3)
        return finish(*args, **kwargs)

    monkeypatch.setattr(database, "finish_background_run", slow_write)
    first = service.submit("maigret", "user")
    try:
        assert started.wait(3)
        second = service.submit("maigret", "another")
        assert second != first
    finally:
        release.set()
    assert wait_for_run(service, second)["status"] == "success"

def test_idempotency_key_remains_persistent_after_completion(service):
    first = None
    for index in range(5):
        run_id = service.submit("maigret", "user", f"key-{index}")
        if index == 0:
            first = run_id
        assert wait_for_run(service, run_id)["status"] == "success"
    assert service.submit("maigret", "user", "key-0") == first


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


@pytest.mark.parametrize(
    ("error", "prefix"),
    [(ValueError, b"F"), (TimeoutError, b"T")],
)
def test_worker_redacts_sensitive_failure(monkeypatch, error, prefix):
    sensitive_value = "person@example.com"
    if os.name == "posix":
        monkeypatch.setattr(os, "setsid", lambda: None)
    runner = Mock(side_effect=error(f"Lookup for {sensitive_value} failed"))
    monkeypatch.setitem(background_runs._TOOLS, "holehe", (lambda value: value, runner))
    connection = Mock()

    background_runs._worker(connection, "holehe", sensitive_value, 1024)

    runner.assert_called_once_with(sensitive_value)
    connection.send_bytes.assert_called_once_with(prefix + b"Lookup for [redacted] failed")
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


def test_spooled_outcome_replays_after_database_restart(service, monkeypatch):
    service.close()
    repo = Path(__file__).resolve().parents[1]
    script = """
import os, sys, time
sys.path.insert(0, 'src')
sys.path.insert(0, 'tests')
import background_runs
from data.utils import database
import test_background_runs
database.setup_database()
background_runs._worker = test_background_runs.durable_worker
database.finish_background_run = lambda *a, **k: (_ for _ in ()).throw(
    OSError('database unavailable')
)
service = background_runs.BackgroundRuns(
    background_runs.RunLimits(timeout_seconds=5)
)
run_id = service.submit('maigret', 'user', 'restart-key')
print(run_id, flush=True)
deadline = time.monotonic() + 10
while not list(service._spool_dir.glob(f'run-{run_id}-*.json')):
    if time.monotonic() >= deadline:
        raise RuntimeError('worker did not create its durable outcome')
    time.sleep(0.01)
os._exit(0)
"""
    env = os.environ.copy()
    env["OSINTBEAST_DB_PATH"] = str(database.DB_FILE)
    env["OSINTBEAST_RUN_SPOOL_DIR"] = str(service._spool_dir)
    process = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=True,
    )
    run_id = int(process.stdout.strip())
    spool_files = list(service._spool_dir.glob(f"run-{run_id}-*.json"))
    assert len(spool_files) == 1
    assert database.get_tool_run(run_id)[3] == "running"

    restarted = background_runs.BackgroundRuns(service.limits)
    try:
        assert database.get_tool_run(run_id)[3:5] == (
            "success", "durable output"
        )
        assert len(database.list_tool_results()) == 1
        assert not spool_files[0].exists()
    finally:
        restarted.close()


def test_sensitive_running_run_is_not_replayed_after_restart(service):
    service.close()
    repo = Path(__file__).resolve().parents[1]
    script = """
import os, sys, time
sys.path.insert(0, 'src')
sys.path.insert(0, 'tests')
import background_runs
from data.utils import database
import test_background_runs
database.setup_database()
background_runs._worker = test_background_runs.blocked_worker
service = background_runs.BackgroundRuns(
    background_runs.RunLimits(timeout_seconds=60)
)
run_id = service.submit('holehe', 'person@example.com', 'sensitive-restart')
print(run_id, flush=True)
os._exit(0)
"""
    env = os.environ.copy()
    env["OSINTBEAST_DB_PATH"] = str(database.DB_FILE)
    env["OSINTBEAST_RUN_SPOOL_DIR"] = str(service._spool_dir)
    process = subprocess.run(
        [sys.executable, "-c", script],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=True,
    )
    run_id = int(process.stdout.strip())
    assert database.get_tool_run(run_id)[2] == "[redacted]"
    with database.connect_db() as connection:
        connection.execute(
            "UPDATE ToolRunExecutions SET leaseExpiresAt = ? WHERE runID = ?",
            (background_runs._now_ms() - 1, run_id),
        )

    restarted = background_runs.BackgroundRuns(service.limits)
    try:
        assert database.get_background_run_state(run_id)[0] == "indeterminate"
        assert database.get_tool_run(run_id)[5] == (
            "Worker outcome is unknown after process termination."
        )
        assert database.list_tool_results() == []
    finally:
        restarted.close()


def test_expired_ordinary_run_is_reclaimed_and_stale_owner_is_fenced(service):
    run_id, created, attempt = database.create_background_run(
        "maigret", "user", owner_token="b" * 32,
        lease_expires_at=background_runs._now_ms() - 1,
    )
    assert created and attempt == 1

    service._recover_run(run_id)
    assert wait_for_run(service, run_id)["status"] == "success"
    state = database.get_background_run_state(run_id)
    assert state[:2] == ("success", 2)
    assert not database.renew_background_run(
        run_id, "b" * 32, 1, background_runs._now_ms(),
        background_runs._now_ms() + 60_000,
    )
    assert not database.finish_background_run(
        run_id, "b" * 32, 1, "success", result_text="stale"
    )
    assert database.get_tool_run(run_id)[4] == "output"
    assert len(database.list_tool_results()) == 1


def test_long_worker_renews_lease_while_two_instances_recover(service, monkeypatch):
    owner = "c" * 32
    run_id, created, attempt = database.create_background_run(
        "maigret", "user", owner_token=owner,
        lease_expires_at=background_runs._now_ms() + 100,
    )
    assert created and attempt == 1
    monkeypatch.setattr(background_runs, "_worker", sleeping_worker)
    monkeypatch.setattr(background_runs, "_LEASE_RENEW_INTERVAL_SECONDS", 0.03)
    cancel, completed = threading.Event(), threading.Event()
    supervisor = threading.Thread(
        target=service._supervise,
        args=(
            run_id, "maigret", "user", cancel, completed,
            owner, attempt, background_runs._now_ms() + 100,
        ),
    )
    supervisor.start()
    time.sleep(0.15)
    second = background_runs.BackgroundRuns(service.limits)
    third = background_runs.BackgroundRuns(service.limits)
    try:
        state = database.get_background_run_state(run_id)
        assert state[0] == "running"
        assert state[1] == attempt
        assert state[2] == owner
        assert state[3] > background_runs._now_ms()
    finally:
        second.close()
        third.close()
    supervisor.join(5)
    assert not supervisor.is_alive()
    assert database.get_tool_run(run_id)[3:5] == (
        "success", "renewed output"
    )
    assert database.get_background_run_state(run_id)[:2] == ("success", 1)


def test_two_instances_replay_same_spool_outcome_once(service):
    service.close()
    owner = "e" * 32
    run_id, created, attempt = database.create_background_run(
        "maigret", "user", owner_token=owner,
        lease_expires_at=background_runs._now_ms() + 60_000,
    )
    assert created and attempt == 1
    path = background_runs._write_spooled_outcome(
        service._spool_dir, run_id, attempt, owner, "success", "shared outcome"
    )

    def recover():
        return background_runs.BackgroundRuns(service.limits)

    with ThreadPoolExecutor(max_workers=2) as pool:
        instances = list(pool.map(lambda _: recover(), range(2)))
    try:
        assert database.get_tool_run(run_id)[4] == "shared outcome"
        assert len(database.list_tool_results()) == 1
        assert not path.exists()
    finally:
        for instance in instances:
            instance.close()


def test_spool_rejects_malformed_records_mismatched_names_and_symlinks(service):
    owner = "f" * 32
    malformed = service._spool_dir / f"run-1-1-{owner}.json"
    malformed.write_text(
        json.dumps({
            "run_id": 2, "attempt": 1, "owner_token": owner,
            "status": "success", "value": "wrong run",
        }),
        encoding="utf-8",
    )
    malformed.chmod(0o600)
    with pytest.raises(ValueError, match="mismatched"):
        service._read_spool(malformed)
    malformed.unlink()

    target = service._spool_dir / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = service._spool_dir / f"run-1-1-{owner}.json"
    link.symlink_to(target)
    try:
        with pytest.raises(OSError):
            service._read_spool(link)
    finally:
        link.unlink()
        target.unlink()


def test_spool_file_remains_after_database_persistence_failure(service, monkeypatch):
    owner = "a" * 32
    run_id, created, attempt = database.create_background_run(
        "maigret", "user", owner_token=owner,
        lease_expires_at=background_runs._now_ms() + 60_000,
    )
    assert created and attempt == 1
    path = background_runs._write_spooled_outcome(
        service._spool_dir, run_id, attempt, owner, "success", "pending"
    )
    monkeypatch.setattr(
        database, "finish_background_run",
        Mock(side_effect=sqlite3.OperationalError("database unavailable")),
    )
    with pytest.raises(sqlite3.OperationalError, match="unavailable"):
        service._persist_spool_file(path)
    assert path.exists()


@pytest.mark.parametrize("status", ["success", "failed", "timed_out"])
def test_late_spooled_outcome_after_cancellation_is_reconciled(service, status):
    service.close()
    owner = "a" * 32
    run_id, _, attempt = database.create_background_run(
        "maigret", "user", owner_token=owner,
        lease_expires_at=background_runs._now_ms() + 60_000,
    )
    assert database.cancel_background_run(run_id)
    path = background_runs._write_spooled_outcome(
        service._spool_dir, run_id, attempt, owner, status, "late outcome"
    )

    assert service._persist_spool_file(path)
    assert not path.exists()
    assert database.get_tool_run(run_id)[3:6] == ("failed", None, "Run cancelled.")
    assert database.list_tool_results() == []


@pytest.mark.parametrize("mismatch", ["attempt", "error", "terminal"])
def test_mismatched_spool_is_not_deleted(service, mismatch):
    service.close()
    owner = "b" * 32
    run_id, _, attempt = database.create_background_run(
        "maigret", "user", owner_token=owner,
        lease_expires_at=background_runs._now_ms() + 60_000,
    )
    if mismatch == "terminal":
        assert database.finish_background_run(
            run_id, owner, attempt, "failed", error_text="different failure"
        )
    else:
        assert database.cancel_background_run(run_id)
    if mismatch == "error":
        with database.connect_db() as connection:
            connection.execute(
                "UPDATE ToolRuns SET errorText = ? WHERE runID = ?",
                ("different error", run_id),
            )
            connection.commit()
    path = background_runs._write_spooled_outcome(
        service._spool_dir, run_id, attempt + (mismatch == "attempt"),
        owner, "success", "late outcome",
    )

    assert not service._persist_spool_file(path)
    assert path.exists()
    assert database.list_tool_results() == []


def test_retry_processing_does_not_repeat_reconciled_cancellation(service, monkeypatch):
    service.close()
    owner = "c" * 32
    run_id, _, attempt = database.create_background_run(
        "maigret", "user", owner_token=owner,
        lease_expires_at=background_runs._now_ms() + 60_000,
    )
    assert database.cancel_background_run(run_id)
    path = background_runs._write_spooled_outcome(
        service._spool_dir, run_id, attempt, owner, "failed", "late failure"
    )
    finish = Mock(wraps=database.finish_background_run)
    monkeypatch.setattr(database, "finish_background_run", finish)

    assert not service._process_spooled_outcomes()
    assert not path.exists()
    assert not service._process_spooled_outcomes()
    assert finish.call_count == 1
