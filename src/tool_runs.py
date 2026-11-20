import logging
import re
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from urllib.parse import quote, quote_plus

import data.utils.database as database


def _redact_sensitive_value(text: str, sensitive_value: str) -> str:
    """Redact common direct and URL-encoded representations of an input."""
    variants = {
        sensitive_value,
        quote(sensitive_value, safe=""),
        quote_plus(sensitive_value),
    }
    for variant in sorted(variants, key=len, reverse=True):
        if variant:
            text = re.sub(re.escape(variant), "[redacted]", text, flags=re.IGNORECASE)
    return text


def execute_tool_run(
    tool_name: str,
    query_text: str,
    runner: Callable[[str], str],
    *,
    sensitive_input: bool = False,
) -> tuple[str, str]:
    """Persist the run outcome; propagate persistence errors to the caller."""
    stored_query = "[redacted]" if sensitive_input else query_text
    run_id = database.start_tool_run(tool_name, stored_query)
    if run_id is None:
        raise RuntimeError("Failed to start tool run.")
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
        if not sensitive_input:
            raise
        message = str(exc) or f"{tool_name} failed ({type(exc).__name__})."
        message = _redact_sensitive_value(message, query_text)
        database.finish_tool_run(run_id, "failed", error_text=message)
        return "failed", message
    if sensitive_input:
        output = _redact_sensitive_value(output, query_text)
    database.finish_tool_run(run_id, "success", result_text=output)
    return "success", output
