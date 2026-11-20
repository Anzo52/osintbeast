"""Narrow application services for backend and future web-facing callers."""

from data.utils.database import (
    list_tool_results as _list_tool_results,
    list_tool_runs as _list_tool_runs,
)

__all__ = ("get_recent_tool_runs", "get_saved_tool_results")


def get_recent_tool_runs(limit: int = 20):
    """Return a bounded page of recent tool runs."""
    return _list_tool_runs(limit)


def get_saved_tool_results(limit: int = 20):
    """Return a bounded page of saved tool results."""
    return _list_tool_results(limit)
