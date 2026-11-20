"""Bounded, process-isolated execution for use by a web application."""

import logging
import math
import multiprocessing
import multiprocessing.connection
import os
import signal
import threading
import time
from dataclasses import dataclass

import data.utils.database as database
from helix_integration import run_helix, validate_username as validate_helix
from holehe_integration import run_holehe, validate_email
from maigret_integration import run_maigret, validate_username as validate_maigret
from sherlock_integration import run_sherlock, validate_username as validate_sherlock

_LOG = logging.getLogger(__name__)
_TOOLS = {
    "holehe": (validate_email, run_holehe),
    "maigret": (validate_maigret, run_maigret),
    "helix": (validate_helix, run_helix),
    "sherlock": (validate_sherlock, run_sherlock),
}


class CapacityError(RuntimeError):
    """The configured number of active runs has been reached."""


@dataclass(frozen=True)
class RunLimits:
    max_concurrent: int = 2
    timeout_seconds: float = 300
    max_query_chars: int = 254
    max_output_bytes: int = 1_000_000

    def __post_init__(self):
        for value in (self.max_concurrent, self.max_query_chars, self.max_output_bytes):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("Run limits must be positive integers.")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be finite and positive.")


def _watch_parent(parent_sentinel, deadline):
    remaining = max(0, deadline - time.monotonic())
    multiprocessing.connection.wait([parent_sentinel], timeout=remaining)
    if os.name == "posix":
        os.killpg(os.getpgrp(), signal.SIGKILL)
    else:
        os._exit(1)


def _worker(connection, tool_name, query, max_output_bytes, deadline=None):
    try:
        if os.name == "posix":
            os.setsid()
        if deadline is not None:
            parent = multiprocessing.parent_process()
            if parent is None:
                raise RuntimeError("Background worker has no parent process.")
            threading.Thread(
                target=_watch_parent, args=(parent.sentinel, deadline), daemon=True
            ).start()
        output = _TOOLS[tool_name][1](query)
        if tool_name == "holehe":
            output = output["output"]
        if not isinstance(output, str):
            raise ValueError("Tool returned an invalid result.")
        payload = output.encode("utf-8")
        if len(payload) > max_output_bytes:
            raise ValueError("Tool output exceeded the configured limit.")
        connection.send_bytes(b"S" + payload)
    except BaseException as exc:
        status = b"T" if isinstance(exc, TimeoutError) else b"F"
        message = str(exc) or f"Worker failed ({type(exc).__name__})."
        try:
            connection.send_bytes(status + message.encode("utf-8")[:max_output_bytes])
        except (BrokenPipeError, OSError):
            pass
    finally:
        connection.close()


def _stop_process(process):
    if process.pid is None:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.is_alive():
        process.kill()
    process.join()


class BackgroundRuns:
    """A per-process service; share one instance between web request handlers."""

    def __init__(self, limits: RunLimits = RunLimits()):
        self.limits = limits
        self._context = multiprocessing.get_context("spawn")
        self._lock = threading.Lock()
        self._jobs = {}
        self._pending = {}
        self._closed = False

    def submit(self, tool_name: str, query: str) -> int:
        if not isinstance(tool_name, str) or tool_name not in _TOOLS:
            raise ValueError("Unsupported tool.")
        if not isinstance(query, str) or len(query) > self.limits.max_query_chars:
            raise ValueError("Query exceeds the configured limit.")
        query = _TOOLS[tool_name][0](query)
        with self._lock:
            if self._closed:
                raise RuntimeError("Background run service is closed.")
            if len(self._jobs) + len(self._pending) >= self.limits.max_concurrent:
                raise CapacityError("Background run capacity reached.")
            run_id = database.start_tool_run(tool_name, query)
            cancel = threading.Event()
            completed = threading.Event()
            thread = threading.Thread(
                target=self._supervise, args=(run_id, tool_name, query, cancel, completed),
                name=f"tool-run-{run_id}", daemon=True,
            )
            self._jobs[run_id] = (cancel, thread, completed)
            try:
                thread.start()
            except BaseException:
                del self._jobs[run_id]
                self._pending[run_id] = ("failed", "Could not start background worker.")
                try:
                    self._persist(run_id, *self._pending[run_id])
                except Exception:
                    _LOG.exception("Could not persist background run startup failure")
                else:
                    del self._pending[run_id]
                raise
        return run_id

    def status(self, run_id: int):
        with self._lock:
            if run_id in self._pending:
                status, value = self._pending[run_id]
                try:
                    self._persist(run_id, status, value)
                except Exception as exc:
                    raise RuntimeError("Could not persist background run outcome.") from exc
                del self._pending[run_id]
        row = database.get_tool_run(run_id)
        if row is None:
            return None
        return dict(zip(
            ("runID", "toolName", "queryText", "status", "resultText", "errorText",
             "startedAt", "finishedAt"),
            row,
        ))

    def cancel(self, run_id: int) -> bool:
        with self._lock:
            job = self._jobs.get(run_id)
            if job is None or job[2].is_set():
                return False
            job[0].set()
            return True

    def close(self):
        with self._lock:
            self._closed = True
            jobs = list(self._jobs.values())
            for cancel, _, completed in jobs:
                if not completed.is_set():
                    cancel.set()
        for _, thread, _ in jobs:
            thread.join()

    @staticmethod
    def _persist(run_id, status, value):
        if status == "success":
            database.finish_tool_run(run_id, status, result_text=value)
        else:
            database.finish_tool_run(run_id, status, error_text=value)

    def _supervise(self, run_id, tool_name, query, cancel, completed):
        process = None
        receiver = sender = None
        status, value = "failed", "Background worker failed."
        try:
            if not cancel.is_set():
                receiver, sender = self._context.Pipe(duplex=False)
                deadline = time.monotonic() + self.limits.timeout_seconds
                process = self._context.Process(
                    target=_worker,
                    args=(sender, tool_name, query, self.limits.max_output_bytes,
                          deadline),
                )
                process.start()
                sender.close()
                sender = None
                while not cancel.is_set():
                    remaining = deadline - time.monotonic()
                    if receiver.poll(max(0, min(remaining, 0.05))):
                        try:
                            payload = receiver.recv_bytes(self.limits.max_output_bytes + 1)
                        except (EOFError, OSError):
                            if time.monotonic() >= deadline:
                                status, value = "timed_out", "Background run exceeded its time limit."
                            break
                        if payload and payload[:1] in (b"S", b"F", b"T"):
                            status = {b"S": "success", b"F": "failed", b"T": "timed_out"}[payload[:1]]
                            value = payload[1:].decode("utf-8", errors="replace")
                            with self._lock:
                                if not cancel.is_set():
                                    completed.set()
                        break
                    if remaining <= 0:
                        status, value = "timed_out", "Background run exceeded its time limit."
                        break
                    if not process.is_alive():
                        if time.monotonic() >= deadline:
                            status, value = "timed_out", "Background run exceeded its time limit."
                        break
        except Exception:
            _LOG.exception("Background worker could not be supervised")
        finally:
            if process is not None:
                try:
                    _stop_process(process)
                except Exception:
                    _LOG.exception("Could not stop background worker")
                    status, value = "failed", "Could not stop background worker."
            if receiver is not None:
                receiver.close()
            if sender is not None:
                sender.close()
            with self._lock:
                if cancel.is_set() and not completed.is_set():
                    status, value = "failed", "Run cancelled."
                try:
                    self._persist(run_id, status, value)
                except Exception:
                    _LOG.exception("Could not persist background run outcome")
                    self._pending[run_id] = (status, value)
                finally:
                    self._jobs.pop(run_id, None)
