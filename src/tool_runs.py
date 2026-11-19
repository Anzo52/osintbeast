from collections.abc import Callable

import data.utils.database as database


def execute_tool_run(
    tool_name: str, query_text: str, runner: Callable[[str], str]
) -> tuple[str, str]:
    """Persist the run outcome; propagate persistence errors to the caller."""
    run_id = database.start_tool_run(tool_name, query_text)
    try:
        output = runner(query_text)
    except TimeoutError as exc:
        message = str(exc) or f"{tool_name} exceeded its time limit."
        database.finish_tool_run(run_id, "timed_out", error_text=message)
        return "timed_out", message
    except (RuntimeError, OSError, ImportError, ValueError) as exc:
        message = str(exc) or f"{tool_name} failed ({type(exc).__name__})."
        database.finish_tool_run(run_id, "failed", error_text=message)
        return "failed", message
    database.finish_tool_run(run_id, "success", result_text=output)
    return "success", output
