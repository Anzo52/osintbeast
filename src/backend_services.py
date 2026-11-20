"""Narrow application services for backend and future web-facing callers."""

from typing import NamedTuple

from data.utils.database import (
    list_tool_results as _list_tool_results,
    list_tool_runs as _list_tool_runs,
)

MAX_SAVED_RESULT_PAGE_SIZE = 20
MAX_SAVED_RESULT_TEXT_CHARS = 16_384


class RecentToolRun(NamedTuple):
    run_id: int
    tool_name: str
    query_text: str
    status: str
    started_at: str
    finished_at: str | None
    error_text: str | None


class SavedToolResult(NamedTuple):
    result_id: int
    tool_name: str
    query_text: str
    result_text: str | None
    created_at: str


__all__ = (
    "RecentToolRun",
    "SavedToolResult",
    "get_recent_tool_runs",
    "get_saved_tool_results",
)


def get_recent_tool_runs(limit: int = 20) -> list[RecentToolRun]:
    """Return a bounded page of named recent tool runs."""
    return [RecentToolRun(*row) for row in _list_tool_runs(limit)]


def get_saved_tool_results(limit: int = 20) -> list[SavedToolResult]:
    """Return a bounded page of saved results with capped output text."""
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ValueError("limit must be between 1 and 20")
    if not 1 <= limit <= MAX_SAVED_RESULT_PAGE_SIZE:
        raise ValueError("limit must be between 1 and 20")
    rows = _list_tool_results(
        limit, max_result_text_chars=MAX_SAVED_RESULT_TEXT_CHARS
    )
    return [SavedToolResult(*row) for row in rows]
