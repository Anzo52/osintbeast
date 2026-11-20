"""Narrow application services for backend and future web-facing callers."""

import sqlite3
import threading

import data.utils.database as database
from data.utils.database import (
    list_tool_results as _list_tool_results,
    list_tool_runs as _list_tool_runs,
)
from tool_runs import execute_tool_run

__all__ = ("get_recent_tool_runs", "get_saved_tool_results", "BackgroundRunService")


class RunCapacityError(Exception):
    """The background run limit has been reached."""


class RunStartError(Exception):
    """A background worker could not be started."""


class BackgroundRunService:
    """Submit runs without exceeding a fixed number of background workers."""

    def __init__(self, max_workers=2):
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        self._slots = threading.BoundedSemaphore(max_workers)

    def submit(self, tool_name, query_text, runner):
        if not self._slots.acquire(blocking=False):
            raise RunCapacityError("Too many runs are active. Try again later.")
        try:
            run_id = database.start_tool_run(tool_name, query_text)
            try:
                thread = threading.Thread(
                    target=self._run,
                    args=(run_id, tool_name, query_text, runner),
                    daemon=True,
                )
                thread.start()
            except Exception as exc:
                database.finish_tool_run(
                    run_id, "failed", error_text="The tool run could not be started."
                )
                raise RunStartError("The tool run could not be started.") from exc
        except Exception:
            self._slots.release()
            raise
        return run_id

    def _run(self, run_id, tool_name, query_text, runner):
        try:
            execute_tool_run(tool_name, query_text, runner, run_id=run_id)
        except Exception as exc:
            try:
                database.finish_tool_run(
                    run_id,
                    "failed",
                    error_text=str(exc) or "The tool run could not be completed.",
                )
            except (sqlite3.Error, ValueError):
                pass
        finally:
            self._slots.release()


def get_recent_tool_runs(limit: int = 20):
    """Return a bounded page of recent tool runs."""
    return _list_tool_runs(limit)


def get_saved_tool_results(limit: int = 20):
    """Return a bounded page of saved tool results."""
    return _list_tool_results(limit)
