import json
import logging
import re
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO

import data.utils.database as database


def _redact_sensitive_value(text: str, sensitive_value: str) -> str:
    """Redact whole input values, including partially URL-encoded forms."""
    if not sensitive_value:
        return text
    alternatives = []
    for char in sensitive_value:
        encoded = "".join(f"%{byte:02X}" for byte in char.encode("utf-8"))
        representations = [re.escape(char), re.escape(encoded)]
        if char == " ":
            representations.append(r"\+")
        alternatives.append(f"(?:{'|'.join(representations)})")
    pattern = re.compile(
        rf"(?<![A-Za-z0-9_.-])(?<!\[){''.join(alternatives)}"
        r"(?![A-Za-z0-9_.-])(?!\])",
        re.IGNORECASE,
    )
    return pattern.sub("[redacted]", text)


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
    tool_name: str, query_text: str, *, sensitive_input: bool = False
):
    """Create a run record, redacting its input when it is sensitive."""
    stored_query = "[redacted]" if sensitive_input else query_text
    return database.start_tool_run(tool_name, stored_query)


def execute_tool_run(
    tool_name: str,
    query_text: str,
    runner: Callable[[str], str],
    run_id: int | None = None,
    *,
    sensitive_input: bool = False,
) -> tuple[str, str]:
    """Persist the run outcome; propagate persistence errors to the caller."""
    if run_id is None:
        run_id = start_tool_run(
            tool_name, query_text, sensitive_input=sensitive_input
        )
    if run_id is None:
        raise RuntimeError("Failed to start tool run.")
    return _execute_tool_run(
        tool_name, query_text, runner, run_id, sensitive_input=sensitive_input
    )


def execute_tool_run_with_id(
    tool_name: str, query_text: str, runner: Callable[[str], str],
    *, sensitive_input: bool = False,
) -> tuple[int, str, str]:
    """Execute a run and return its persisted identifier and outcome."""
    run_id = start_tool_run(
        tool_name, query_text, sensitive_input=sensitive_input
    )
    if run_id is None:
        raise RuntimeError("Failed to start tool run.")
    status, value = _execute_tool_run(
        tool_name, query_text, runner, run_id, sensitive_input=sensitive_input
    )
    return run_id, status, value


def _execute_tool_run(
    tool_name: str,
    query_text: str,
    runner: Callable[[str], str],
    run_id: int,
    *,
    sensitive_input: bool = False,
) -> tuple[str, str]:
    """Execute and persist a run using an existing run identifier."""
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
        message = (
            str(exc) or f"{tool_name} failed ({type(exc).__name__})."
            if sensitive_input
            else f"{tool_name} failed unexpectedly."
        )
        if sensitive_input:
            message = _redact_sensitive_value(message, query_text)
        database.finish_tool_run(run_id, "failed", error_text=message)
        return "failed", message
    if sensitive_input:
        output = redact_sensitive_output(output, query_text)
    database.finish_tool_run(run_id, "success", result_text=output)
    return "success", output
