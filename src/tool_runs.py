import hashlib
import json
import logging
import multiprocessing
import multiprocessing.connection
import os
import signal
import sys
import threading
import traceback
from collections.abc import Callable
from contextlib import contextmanager

import cloudpickle

from data.utils import database

REDACTED = "[redacted]"
_LOG = logging.getLogger(__name__)


@contextmanager
def _defer_start_interrupt():
    """Defer Ctrl+C until the committed run ID has reached its caller."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous_handler = signal.getsignal(signal.SIGINT)
    interrupted = False

    def defer(signum, frame):
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGINT, defer)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous_handler)
        if interrupted:
            signal.raise_signal(signal.SIGINT)


class UnexpectedToolError(RuntimeError):
    """A sanitized unexpected failure from a sensitive tool run."""

    def __init__(self, message, remote_frames=()):
        super().__init__(message)
        self.remote_frames = tuple(remote_frames)


def discard_isolated_process_output():
    """Keep an isolated tool worker's standard streams out of application output."""
    logging.disable(sys.maxsize)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (AttributeError, OSError):
            pass
    null_fd = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null_fd, 1)
        os.dup2(null_fd, 2)
    finally:
        if null_fd not in (1, 2):
            os.close(null_fd)


def _sensitive_runner_worker(connection, serialized_runner, query_text):
    discard_isolated_process_output()
    if os.name == "posix":
        os.setsid()
        parent = multiprocessing.parent_process()
        if parent is not None:
            threading.Thread(
                target=_watch_sensitive_parent, args=(parent.sentinel,), daemon=True
            ).start()
    try:
        runner = cloudpickle.loads(serialized_runner)
        try:
            result = runner(query_text)
        except KeyboardInterrupt:
            response = ("interrupt",)
        except TimeoutError as exc:
            response = (
                "timeout", type(exc).__name__, _exception_text(exc),
                _traceback_frame_names(exc),
            )
        except (RuntimeError, OSError, ImportError, ValueError) as exc:
            response = (
                "failure", type(exc).__name__, _exception_text(exc),
                _traceback_frame_names(exc),
            )
        except Exception as exc:  # noqa: BLE001 - return the tool's failure across the process boundary.
            response = (
                "unexpected", type(exc).__name__, _exception_text(exc),
                _traceback_frame_names(exc),
            )
        else:
            if not isinstance(result, str):
                response = ("non_text",)
            else:
                response = ("result", result)
        if response[0] in {"timeout", "failure", "unexpected"}:
            response = (
                response[0],
                _redact_sensitive_value(response[1], query_text),
                _redact_sensitive_value(response[2], query_text),
                tuple(_redact_sensitive_value(name, query_text) for name in response[3]),
            )
        connection.send_bytes(json.dumps(response, ensure_ascii=True).encode("utf-8"))
    finally:
        connection.close()


def _watch_sensitive_parent(sentinel):
    multiprocessing.connection.wait([sentinel])
    os.killpg(os.getpgrp(), signal.SIGKILL)


def _exception_text(exc):
    try:
        return str(exc)
    except Exception:  # noqa: BLE001 - exception stringification must not escape the worker.
        return ""


def _traceback_frame_names(exc):
    return tuple(
        frame.name for frame in traceback.extract_tb(exc.__traceback__)
    )


@contextmanager
def _sensitive_sherlock_slot(tool_name):
    if tool_name != "sherlock":
        yield
        return
    from sherlock_integration import _WORKER_SLOTS

    if not _WORKER_SLOTS.acquire(blocking=False):
        raise RuntimeError("Sherlock is busy; try again later.")
    try:
        yield
    finally:
        _WORKER_SLOTS.release()


def _run_sensitive_runner(tool_name, runner, query_text):
    context = multiprocessing.get_context("spawn")
    try:
        serialized_runner = cloudpickle.dumps(runner)
    except Exception as exc:
        raise RuntimeError("Could not isolate sensitive tool runner.") from exc
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(
        target=_sensitive_runner_worker,
        args=(sender, serialized_runner, query_text),
        name="sensitive-tool-run",
    )
    try:
        process.start()
        sender.close()
        try:
            response = json.loads(receiver.recv_bytes(0x7FFFFFFF))
        except EOFError as exc:
            raise RuntimeError(
                "Sensitive tool worker exited without returning an outcome."
            ) from exc
    finally:
        receiver.close()
        sender.close()
        if process.pid is not None:
            process.join(timeout=1)
            if process.is_alive():
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        process.terminate()
                else:
                    process.terminate()
            process.join()
    if not isinstance(response, list) or not response:
        raise RuntimeError("Sensitive tool worker returned an invalid outcome.")
    if response[0] == "result" and len(response) == 2 and isinstance(response[1], str):
        return response[1]
    if response == ["non_text"]:
        raise RuntimeError(f"{tool_name} returned non-text output.")
    if response == ["interrupt"]:
        raise KeyboardInterrupt
    if len(response) != 4 or response[0] not in {
        "timeout", "failure", "unexpected"
    }:
        raise RuntimeError("Sensitive tool worker returned an invalid outcome.")
    _, exception_type, message, remote_frames = response
    if not isinstance(exception_type, str) or not isinstance(message, str):
        raise RuntimeError("Sensitive tool worker returned an invalid outcome.")  # noqa: TRY004
    if response[0] == "timeout":
        raise TimeoutError(message or f"Tool exceeded its time limit ({exception_type}).")
    if response[0] == "failure":
        error = {
            "RuntimeError": RuntimeError,
            "OSError": OSError,
            "ImportError": ImportError,
            "ValueError": ValueError,
        }.get(exception_type, RuntimeError)
        raise error(message or f"Tool failed ({exception_type}).")
    raise UnexpectedToolError(
        f"{tool_name} failed unexpectedly ({exception_type}).",
        remote_frames=remote_frames,
    )


def _redact_sensitive_value(text: str, sensitive_value: str) -> str:
    """Redact whole input values, including partially URL-encoded forms."""
    return database.redact_sensitive_text(text, sensitive_value)


def redact_sensitive_data(value, sensitive_value: str):
    """Redact string values and keys in structured data."""
    if isinstance(value, str):
        return _redact_sensitive_value(value, sensitive_value)
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            redacted_key = _redact_sensitive_value(key, sensitive_value) if isinstance(
                key, str
            ) else key
            if redacted_key in result:
                raise ValueError("Sensitive output contains conflicting redacted keys.")
            result[redacted_key] = redact_sensitive_data(item, sensitive_value)
        return result
    if isinstance(value, list):
        return [redact_sensitive_data(item, sensitive_value) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive_data(item, sensitive_value) for item in value)
    return value


def redact_sensitive_output(output: str, sensitive_value: str) -> str:
    """Redact JSON values as data, falling back to text for other output."""
    try:
        structured = json.loads(output)
    except (TypeError, json.JSONDecodeError):
        return _redact_sensitive_value(output, sensitive_value)
    if isinstance(structured, (dict, list)):
        return json.dumps(
            redact_sensitive_data(structured, sensitive_value),
            ensure_ascii=False,
            indent=2,
        )
    return _redact_sensitive_value(output, sensitive_value)


def start_tool_run(
    tool_name: str, query_text: str, *, sensitive_input: bool
):
    """Create a run record, redacting its input when it is sensitive."""
    if not isinstance(sensitive_input, bool):
        raise TypeError("sensitive_input must be a boolean.")
    stored_query = REDACTED if sensitive_input else query_text
    return database.start_tool_run(tool_name, stored_query)


def start_idempotent_tool_run(
    tool_name: str,
    query_text: str,
    idempotency_key: str,
    *,
    sensitive_input: bool,
) -> tuple[int, bool]:
    """Create or return the run associated with one launch request."""
    if not isinstance(sensitive_input, bool):
        raise TypeError("sensitive_input must be a boolean.")
    stored_query = REDACTED if sensitive_input else query_text
    return database.start_idempotent_tool_run(
        tool_name,
        stored_query,
        idempotency_key,
        idempotent_request_hash(tool_name, query_text),
    )


def idempotent_request_hash(tool_name: str, query_text: str) -> str:
    request_data = json.dumps(
        [tool_name, query_text], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(request_data).hexdigest()


def execute_tool_run(
    tool_name: str,
    query_text: str,
    runner: Callable[[str], str],
    run_id: int | None = None,
    *,
    sensitive_input: bool,
) -> tuple[str, str]:
    """Persist the run outcome; propagate persistence errors to the caller."""
    validated_run_id = None
    try:
        if not isinstance(sensitive_input, bool):
            raise TypeError("sensitive_input must be a boolean.")
        if run_id is None:
            with _defer_start_interrupt():
                run_id = start_tool_run(
                    tool_name, query_text, sensitive_input=sensitive_input
                )
                validated_run_id = run_id
        elif sensitive_input:
            run = database.get_tool_run(run_id)
            if run is None or run[1:4] != (tool_name, REDACTED, "running"):
                raise ValueError("Sensitive runs require a redacted running record.")
        if run_id is None:
            raise RuntimeError("Failed to start tool run.")
        validated_run_id = run_id
        return _execute_tool_run(
            tool_name, query_text, runner, run_id, sensitive_input=sensitive_input
        )
    except KeyboardInterrupt:
        if validated_run_id is not None:
            _mark_run_interrupted(validated_run_id)
        raise


def execute_tool_run_with_id(
    tool_name: str, query_text: str, runner: Callable[[str], str],
    *, sensitive_input: bool,
) -> tuple[int, str, str]:
    """Execute a run and return its persisted identifier and outcome."""
    run_id = None
    try:
        with _defer_start_interrupt():
            run_id = start_tool_run(
                tool_name, query_text, sensitive_input=sensitive_input
            )
        if run_id is None:
            raise RuntimeError("Failed to start tool run.")
        status, value = _execute_tool_run(
            tool_name, query_text, runner, run_id, sensitive_input=sensitive_input
        )
        return run_id, status, value
    except KeyboardInterrupt:
        if run_id is not None:
            _mark_run_interrupted(run_id)
        raise


def _mark_run_interrupted(run_id: int) -> None:
    try:
        database.finish_tool_run(
            run_id, "failed", error_text="Tool run interrupted."
        )
    except BaseException as exc:
        try:
            _LOG.error(
                "Could not persist interrupted tool run %s (%s)",
                run_id, type(exc).__name__,
            )
        except BaseException:
            pass


def _execute_tool_run(
    tool_name: str,
    query_text: str,
    runner: Callable[[str], str],
    run_id: int,
    *,
    sensitive_input: bool,
) -> tuple[str, str]:
    """Execute and persist a run using an existing run identifier."""
    return _run_tool_and_persist(
        tool_name, query_text, runner, run_id, sensitive_input=sensitive_input
    )


def _run_tool_and_persist(
    tool_name: str,
    query_text: str,
    runner: Callable[[str], str],
    run_id: int,
    *,
    sensitive_input: bool,
) -> tuple[str, str]:
    try:
        if sensitive_input:
            with _sensitive_sherlock_slot(tool_name):
                output = _run_sensitive_runner(tool_name, runner, query_text)
        else:
            output = runner(query_text)
        if not isinstance(output, str):
            raise RuntimeError(f"{tool_name} returned non-text output.")  # noqa: TRY004
        if sensitive_input:
            output = redact_sensitive_output(output, query_text)
    except UnexpectedToolError as exc:
        database.finish_tool_run(run_id, "failed", error_text=str(exc))
        raise
    except TimeoutError as exc:
        message = str(exc) or f"{tool_name} exceeded its time limit."
        if sensitive_input:
            message = _redact_sensitive_value(message, query_text)
        database.finish_tool_run(run_id, "timed_out", error_text=message)
        return "timed_out", message
    except (RuntimeError, OSError, ImportError, ValueError) as exc:
        message = str(exc) or f"{tool_name} failed ({type(exc).__name__})."
        if sensitive_input:
            message = _redact_sensitive_value(message, query_text)
        database.finish_tool_run(run_id, "failed", error_text=message)
        return "failed", message
    except Exception as exc:
        message = f"{tool_name} failed unexpectedly ({type(exc).__name__})."
        database.finish_tool_run(run_id, "failed", error_text=message)
        if sensitive_input:
            raise UnexpectedToolError(message).with_traceback(exc.__traceback__) from None
        raise
    database.finish_tool_run(run_id, "success", result_text=output)
    return "success", output
