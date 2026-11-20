from collections.abc import Callable

import data.utils.database as database

REDACTED = "[redacted]"


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
    if run_id is None:
        run_id = database.start_tool_run(
            tool_name, REDACTED if sensitive_input else query_text
        )
    elif sensitive_input:
        run = database.get_tool_run(run_id)
        if run is None or run[1:4] != (tool_name, REDACTED, "running"):
            raise ValueError("Sensitive runs require a redacted running record.")
    if run_id is None:
        raise RuntimeError("Failed to start tool run.")
    try:
        output = runner(query_text)
    except TimeoutError as exc:
        message = str(exc) or f"{tool_name} exceeded its time limit."
        database.finish_tool_run(
            run_id, "timed_out", error_text=REDACTED if sensitive_input else message
        )
        return "timed_out", message
    except (RuntimeError, OSError, ImportError, ValueError) as exc:
        message = str(exc) or f"{tool_name} failed ({type(exc).__name__})."
        database.finish_tool_run(
            run_id, "failed", error_text=REDACTED if sensitive_input else message
        )
        return "failed", message
    database.finish_tool_run(
        run_id, "success", result_text=REDACTED if sensitive_input else output
    )
    return "success", output
