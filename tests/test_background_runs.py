import multiprocessing
import os
import sqlite3
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import background_runs
import data.utils.database as database


def mocked_worker(
    connection, tool_name, query, max_output_bytes, deadline=None, timed_out=None
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


def mocked_sensitive_worker(
    connection, _tool_name, query, _max_output_bytes, _deadline=None, _timed_out=None
):
    status = b"F" if query.startswith("failure@") else b"S"
    text = background_runs.redact_sensitive_output(
        f"lookup result for {query}", query
    )
    connection.send_bytes(status + text.encode("utf-8"))
    connection.close()


def blocked_runner(query):
    time.sleep(10)
    return query


def guarded_worker(connection, deadline, ready=None, timed_out=None):
    def runner(query):
        if ready is not None:
            ready.set()
        return blocked_runner(query)

    background_runs._TOOLS["maigret"] = (lambda value: value, runner)
    background_runs._worker(
        connection, "maigret", "user", 100, deadline, timed_out
    )


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
        background_runs, "_worker", mocked_sensitive_worker
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


def test_close_is_bounded_when_final_persistence_stalls(service, monkeypatch, caplog):
    entered, release = threading.Event(), threading.Event()
    finish = database.finish_tool_run

    def stalled_finish(*args, **kwargs):
        entered.set()
        release.wait(5)
        return finish(*args, **kwargs)

    monkeypatch.setattr(database, "finish_tool_run", stalled_finish)
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
    monkeypatch.setattr(background_runs, "time", time)
    assert service.status(run_id)["status"] == "success"
    assert late_cancellation == [False]
    receiver.poll.assert_called_once_with(0)


@pytest.mark.parametrize(
    ("payload", "timed_out", "expected_status", "expected_error"),
    [
        (b"Fworker failed", False, "failed", "worker failed"),
        (b"Tworker timeout", False, "timed_out", "worker timeout"),
        (EOFError(), False, "failed", "Background worker failed."),
        (EOFError(), True, "timed_out", "Background run exceeded its time limit."),
        (b"invalid", False, "failed", "Background worker failed."),
    ],
)
def test_terminal_outcomes_win_over_late_cancellation(
    service, monkeypatch, payload, timed_out, expected_status, expected_error
):
    run_id = database.start_tool_run("maigret", "user")
    cancel, completed = threading.Event(), threading.Event()
    service._jobs[run_id] = (cancel, Mock(), completed)
    receiver = Mock()
    receiver.poll.return_value = True
    if isinstance(payload, Exception):
        receiver.recv_bytes.side_effect = payload
    else:
        receiver.recv_bytes.return_value = payload
    context = Mock()
    watchdog_event = threading.Event()
    if timed_out:
        watchdog_event.set()
    context.Event.return_value = watchdog_event
    context.Pipe.return_value = (receiver, Mock())
    monkeypatch.setattr(service, "_context", context)
    late_cancellation = []
    monkeypatch.setattr(
        background_runs,
        "_stop_process",
        lambda process: late_cancellation.append(service.cancel(run_id)),
    )

    service._supervise(run_id, "maigret", "user", cancel, completed)

    run = service.status(run_id)
    assert (run["status"], run["errorText"]) == (expected_status, expected_error)
    assert late_cancellation == [False]


def test_retry_thread_start_failure_retries_pending_outcome_in_supervisor(
    service, monkeypatch
):
    finish = database.finish_tool_run
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("database unavailable")
        finish(*args, **kwargs)

    monkeypatch.setattr(database, "finish_tool_run", fail_once)
    original_thread = threading.Thread

    def thread_factory(*args, **kwargs):
        if kwargs.get("name") == "tool-run-persistence-retry":
            raise RuntimeError("thread unavailable")
        return original_thread(*args, **kwargs)

    monkeypatch.setattr(background_runs.threading, "Thread", thread_factory)
    run_id = service.submit("maigret", "user")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        row = database.get_tool_run(run_id)
        with service._lock:
            pending = run_id in service._pending
        if row[3] == "success" and not pending:
            break
        time.sleep(0.01)
    else:
        pytest.fail("Pending outcome was not retried after retry-thread failure")
    assert calls >= 2


def test_worker_enforces_its_own_deadline():
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    ready = context.Event()
    timed_out = context.Event()
    process = context.Process(
        target=guarded_worker,
        args=(sender, time.monotonic() + 1.5, ready, timed_out),
    )
    try:
        process.start()
        sender.close()
        assert ready.wait(5)
        time.sleep(0.1)
        assert process.is_alive()
        process.join(4)
        assert not process.is_alive()
        assert process.exitcode != 0
        assert timed_out.is_set()
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


def test_thread_start_and_persistence_failure_exposes_run_id(service, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(background_runs.threading, "Thread", Mock(side_effect=RuntimeError("unavailable")))
        patch.setattr(database, "finish_tool_run", Mock(side_effect=OSError("unavailable")))
        with pytest.raises(background_runs.StartupPersistenceError) as error:
            service.submit("maigret", "user", "startup-retry")
        run_id = error.value.run_id
        assert service.submit("maigret", "user", "startup-retry") == run_id
        with pytest.raises(background_runs.IdempotencyConflict):
            service.submit("maigret", "another", "startup-retry")
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
    assert service.status(run_id)["errorText"] == "Could not start background worker."
    assert wait_for_run(service, service.submit("maigret", "another"))["status"] == "success"


def test_startup_persistence_failure_retries_without_status(service, monkeypatch):
    thread_class = threading.Thread
    finish = database.finish_tool_run

    def create_thread(*args, **kwargs):
        if kwargs.get("name", "").startswith("tool-run-") and (
            kwargs["name"] != "tool-run-persistence-retry"
        ):
            raise RuntimeError("unavailable")
        return thread_class(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(background_runs.threading, "Thread", create_thread)
        patch.setattr(database, "finish_tool_run", Mock(side_effect=OSError("unavailable")))
        with pytest.raises(background_runs.StartupPersistenceError) as error:
            service.submit("maigret", "user", "startup-retry")
        run_id = error.value.run_id
    monkeypatch.setattr(database, "finish_tool_run", finish)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with service._lock:
            pending = run_id in service._pending
        if not pending:
            break
        time.sleep(0.01)
    else:
        pytest.fail("Startup failure was not retried")
    assert database.get_tool_run(run_id)[5] == "Could not start background worker."
    assert service.submit("maigret", "user", "startup-retry") == run_id


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
    with monkeypatch.context() as time_mock:
        time_mock.setattr(
            background_runs,
            "time",
            SimpleNamespace(monotonic=Mock(side_effect=[0, 2])),
        )

        def stop(_):
            if outcome in ("timeout", "crash"):
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


def test_cancellation_during_poll_is_not_replaced_by_a_later_result(service, monkeypatch):
    run_id = database.start_tool_run("maigret", "user")
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


@pytest.mark.parametrize("payload", [EOFError(), OSError(), b"invalid"])
def test_cancellation_during_receive_wins_over_invalid_result(
    service, monkeypatch, payload
):
    run_id = database.start_tool_run("maigret", "user")
    cancel, completed = threading.Event(), threading.Event()
    service._jobs[run_id] = (cancel, Mock(), completed)
    receiver, sender, process = Mock(), Mock(), Mock()
    process.pid = 1
    receiver.poll.return_value = True

    def receive(*args):
        assert service.cancel(run_id)
        if isinstance(payload, Exception):
            raise payload
        return payload

    receiver.recv_bytes.side_effect = receive
    monkeypatch.setattr(service._context, "Pipe", lambda **kwargs: (receiver, sender))
    monkeypatch.setattr(service._context, "Process", lambda **kwargs: process)
    monkeypatch.setattr(background_runs, "_stop_process", lambda _: None)

    service._supervise(run_id, "maigret", "user", cancel, completed)

    assert service.status(run_id)["errorText"] == "Run cancelled."


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


def test_submission_idempotency_survives_service_restart(service):
    run_id = service.submit("holehe", "person@example.com", "persistent-key")
    second_service = background_runs.BackgroundRuns(service.limits)
    try:
        assert second_service.submit(
            "holehe", "person@example.com", "persistent-key"
        ) == run_id
        assert len(database.list_tool_runs()) == 1
        stored = database.query_database(
            "SELECT keyHash FROM ToolRunIdempotency"
        )[0][0]
        assert stored != "persistent-key"
    finally:
        second_service.close()


def test_startup_interrupt_is_preserved_and_exposes_run_id(service, monkeypatch):
    finish = database.finish_tool_run
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("database unavailable")
        finish(*args, **kwargs)

    monkeypatch.setattr(database, "finish_tool_run", fail_once)
    monkeypatch.setattr(
        background_runs.threading, "Thread", Mock(side_effect=KeyboardInterrupt())
    )
    monkeypatch.setattr(service, "_start_retry_locked", lambda: False)

    with pytest.raises(KeyboardInterrupt) as exc_info:
        service.submit("maigret", "user")

    assert exc_info.value.run_id == database.list_tool_runs()[0][0]
    assert database.get_tool_run(exc_info.value.run_id)[3] == "failed"


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
