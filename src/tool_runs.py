from collections.abc import Callable

import data.utils.database as database


def execute_tool_run_with_id(
    tool_name: str,
    query_text: str,
    runner: Callable[[str], str],
    run_id: int | None = None,
) -> tuple[int, str, str]:
    """Persist the run outcome; propagate persistence errors to the caller."""
    if run_id is None:
        run_id = database.start_tool_run(tool_name, query_text)
    if run_id is None:
        raise RuntimeError("Failed to start tool run.")
    try:
        output = runner(query_text)
    except TimeoutError as exc:
        message = str(exc) or f"{tool_name} exceeded its time limit."
        database.finish_tool_run(run_id, "timed_out", error_text=message)
        return run_id, "timed_out", message
    except (RuntimeError, OSError, ImportError, ValueError) as exc:
        message = str(exc) or f"{tool_name} failed ({type(exc).__name__})."
        database.finish_tool_run(run_id, "failed", error_text=message)
        return run_id, "failed", message
    database.finish_tool_run(run_id, "success", result_text=output)
    return run_id, "success", output


def execute_tool_run(
    tool_name: str,
    query_text: str,
    runner: Callable[[str], str],
    run_id: int | None = None,
) -> tuple[str, str]:
    """Persist a run outcome and return its status and result or error."""
    _, status, value = execute_tool_run_with_id(
        tool_name, query_text, runner, run_id=run_id
    )
    return status, value
