import hashlib
import json
import logging
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

import data.utils.database as database

REDACTED = "[redacted]"


class UnexpectedToolError(RuntimeError):
    """A sanitized unexpected failure from a sensitive tool run."""


def _redact_sensitive_value(text: str, sensitive_value: str) -> str:
    """Redact whole input values, including partially URL-encoded forms."""
    return database.redact_sensitive_text(text, sensitive_value)


def redact_sensitive_data(value, sensitive_value: str):
    """Redact string values in structured data without changing its schema."""
    if isinstance(value, str):
        return _redact_sensitive_value(value, sensitive_value)
    if isinstance(value, dict):
        return {
            key: redact_sensitive_data(item, sensitive_value)
            for key, item in value.items()
        }
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
    tool_name: str, query_text: str, *, sensitive_input: bool,
    key_hash: str, input_fingerprint: str,
) -> tuple[int, bool]:
    """Create or find a run while keeping the raw idempotency key out of storage."""
    if not isinstance(sensitive_input, bool):
        raise TypeError("sensitive_input must be a boolean.")
    stored_query = REDACTED if sensitive_input else query_text
    return database.start_idempotent_tool_run(
        tool_name, stored_query, key_hash, input_fingerprint
    )


def start_idempotent_request(
    tool_name: str,
    query_text: str,
    idempotency_key: str,
    *,
    sensitive_input: bool,
) -> tuple[int, bool]:
    """Create or return the run associated with one synchronous request."""
    if not isinstance(sensitive_input, bool):
        raise TypeError("sensitive_input must be a boolean.")
    stored_query = REDACTED if sensitive_input else query_text
    return database.start_idempotent_request(
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
    if not isinstance(sensitive_input, bool):
        raise TypeError("sensitive_input must be a boolean.")
    try:
        if run_id is None:
            run_id = start_tool_run(
                tool_name, query_text, sensitive_input=sensitive_input
            )
        elif sensitive_input:
            run = database.get_tool_run(run_id)
            if run is None or run[1:4] != (tool_name, REDACTED, "running"):
                raise ValueError("Sensitive runs require a redacted running record.")
        if run_id is None:
            raise RuntimeError("Failed to start tool run.")
        return _execute_tool_run(
            tool_name, query_text, runner, run_id, sensitive_input=sensitive_input
        )
    except KeyboardInterrupt:
        if run_id is not None:
            _mark_run_interrupted(run_id)
        raise


def execute_tool_run_with_id(
    tool_name: str, query_text: str, runner: Callable[[str], str],
    *, sensitive_input: bool,
) -> tuple[int, str, str]:
    """Execute a run and return its persisted identifier and outcome."""
    run_id = None
    try:
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
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                previous_disable = logging.root.manager.disable
                logging.disable(logging.CRITICAL)
                try:
                    output = runner(query_text)
                finally:
                    logging.disable(previous_disable)
        else:
            output = runner(query_text)
        if not isinstance(output, str):
            raise RuntimeError(f"{tool_name} returned non-text output.")
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
    if sensitive_input:
        output = redact_sensitive_output(output, query_text)
    database.finish_tool_run(run_id, "success", result_text=output)
    return "success", output
