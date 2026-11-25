"""Bounded, process-isolated execution for use by a web application."""

import hashlib
import hmac
import logging
import math
import multiprocessing
import multiprocessing.connection
import os
import signal
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from io import StringIO

import data.utils.database as database
from helix_integration import run_helix, validate_username as validate_helix
from holehe_integration import run_holehe, validate_email
from maigret_integration import run_maigret, validate_username as validate_maigret
from sherlock_integration import run_sherlock, validate_username as validate_sherlock
from tool_runs import (
    redact_sensitive_output,
    start_idempotent_tool_run,
    start_tool_run,
)

_LOG = logging.getLogger(__name__)
_SHUTDOWN_TIMEOUT_SECONDS = 5
_TOOLS = {
    "holehe": (validate_email, run_holehe),
    "maigret": (validate_maigret, run_maigret),
    "helix": (validate_helix, run_helix),
    "sherlock": (validate_sherlock, run_sherlock),
}


class CapacityError(RuntimeError):
    """The configured number of active runs has been reached."""


class StartupPersistenceError(RuntimeError):
    """A startup failure could not be recorded; run_id can be used to retry."""

    def __init__(self, run_id):
        super().__init__("Could not persist background run startup failure.")
        self.run_id = run_id


class IdempotencyConflict(RuntimeError):
    """An idempotency key was reused for a different submission."""


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


def _watch_parent(parent_sentinel, deadline, timed_out):
    remaining = max(0, deadline - time.monotonic())
    if not multiprocessing.connection.wait([parent_sentinel], timeout=remaining):
        timed_out.set()
    if os.name == "posix":
        os.killpg(os.getpgrp(), signal.SIGKILL)
    else:
        os._exit(1)


def _worker(connection, tool_name, query, max_output_bytes, deadline=None, timed_out=None):
    try:
        if os.name == "posix":
            os.setsid()
        if deadline is not None:
            if timed_out is None:
                timed_out = threading.Event()
            parent = multiprocessing.parent_process()
            if parent is None:
                raise RuntimeError("Background worker has no parent process.")
            threading.Thread(
                target=_watch_parent,
                args=(parent.sentinel, deadline, timed_out),
                daemon=True,
            ).start()
        if tool_name == "holehe":
            previous_disable = logging.root.manager.disable
            logging.disable(logging.CRITICAL)
            try:
                with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                    output = _TOOLS[tool_name][1](query)["output"]
            finally:
                logging.disable(previous_disable)
        else:
            output = _TOOLS[tool_name][1](query)
        if not isinstance(output, str):
            raise ValueError("Tool returned an invalid result.")
        if tool_name == "holehe":
            output = redact_sensitive_output(output, query)
        payload = output.encode("utf-8")
        if len(payload) > max_output_bytes:
            raise ValueError("Tool output exceeded the configured limit.")
        connection.send_bytes(b"S" + payload)
    except BaseException as exc:
        status = b"T" if isinstance(exc, TimeoutError) else b"F"
        if isinstance(exc, (TimeoutError, RuntimeError, OSError, ImportError, ValueError)):
            message = str(exc) or f"Worker failed ({type(exc).__name__})."
        else:
            message = f"{tool_name} failed unexpectedly ({type(exc).__name__})."
        if tool_name == "holehe":
            message = redact_sensitive_output(message, query)
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
        self._job_locks = {}
        self._pending = {}
        self._retry_wakeup = threading.Event()
        self._retry_stop = threading.Event()
        self._retry_thread = None
        self._closed = False

    def submit(
        self, tool_name: str, query: str, idempotency_key: str | None = None
    ) -> int:
        if not isinstance(tool_name, str) or tool_name not in _TOOLS:
            raise ValueError("Unsupported tool.")
        if not isinstance(query, str) or len(query) > self.limits.max_query_chars:
            raise ValueError("Query exceeds the configured limit.")
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str)
            or not 1 <= len(idempotency_key) <= 128
            or any(not 33 <= ord(char) <= 126 for char in idempotency_key)
        ):
            raise ValueError("Idempotency-Key must contain 1 to 128 visible ASCII characters.")
        query = _TOOLS[tool_name][0](query)
        key_record = None
        if idempotency_key is not None:
            key_hash = hashlib.sha256(idempotency_key.encode("ascii")).hexdigest()
            fingerprint = hmac.new(
                idempotency_key.encode("ascii"),
                query.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            key_record = (key_hash, fingerprint)
        startup_error = None
        fallback_retry = False
        with self._lock:
            if self._closed:
                raise RuntimeError("Background run service is closed.")
            if key_record is not None:
                existing = database.get_idempotent_tool_run(key_record[0])
                if existing is not None:
                    if existing[1:] != (tool_name, key_record[1]):
                        raise IdempotencyConflict(
                            "Idempotency-Key was already used for a different run."
                        )
                    return existing[0]
            if len(self._jobs) + len(self._pending) >= self.limits.max_concurrent:
                raise CapacityError("Background run capacity reached.")
            if key_record is None:
                run_id = start_tool_run(
                    tool_name, query, sensitive_input=tool_name == "holehe"
                )
            else:
                try:
                    run_id, created = start_idempotent_tool_run(
                        tool_name,
                        query,
                        sensitive_input=tool_name == "holehe",
                        key_hash=key_record[0],
                        input_fingerprint=key_record[1],
                    )
                except ValueError as exc:
                    if "Idempotency-Key" in str(exc):
                        raise IdempotencyConflict(str(exc)) from exc
                    raise
                if not created:
                    return run_id
            cancel = threading.Event()
            completed = threading.Event()
            state_lock = threading.Lock()
            try:
                thread = threading.Thread(
                    target=self._supervise, args=(run_id, tool_name, query, cancel, completed),
                    name=f"tool-run-{run_id}", daemon=True,
                )
                self._jobs[run_id] = (cancel, thread, completed)
                self._job_locks[run_id] = state_lock
                thread.start()
            except BaseException as exc:
                self._jobs.pop(run_id, None)
                self._job_locks.pop(run_id, None)
                self._pending[run_id] = ("failed", "Could not start background worker.")
                try:
                    self._persist(run_id, *self._pending[run_id])
                except Exception as persistence_error:
                    _LOG.exception("Could not persist background run startup failure")
                    fallback_retry = not self._start_retry_locked()
                    if isinstance(exc, Exception):
                        startup_error = StartupPersistenceError(run_id)
                        startup_error.__cause__ = persistence_error
                    else:
                        startup_error = exc
                else:
                    del self._pending[run_id]
                    startup_error = exc
                try:
                    startup_error.run_id = run_id
                except (AttributeError, TypeError):
                    pass
        if fallback_retry:
            self._retry_pending(until_empty=True)
        if startup_error is not None:
            raise startup_error.with_traceback(startup_error.__traceback__)
        return run_id

    def status(self, run_id: int):
        with self._lock:
            if run_id in self._pending:
                status, value = self._pending[run_id]
                try:
                    self._persist(run_id, status, value)
                except Exception as exc:
                    row = database.get_tool_run(run_id) if isinstance(exc, ValueError) else None
                    if row is None or row[3] == "running":
                        raise RuntimeError("Could not persist background run outcome.") from exc
                del self._pending[run_id]
        row = database.get_tool_run(run_id, redact_sensitive=True)
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
            state_lock = self._job_locks.get(run_id)
        if job is None or job[2].is_set():
            return False
        if state_lock is None:
            state_lock = self._lock
        with state_lock:
            if job[2].is_set():
                return False
            job[0].set()
            return True

    def close(self):
        deadline = time.monotonic() + _SHUTDOWN_TIMEOUT_SECONDS
        self._closed = True
        jobs = []
        if not self._lock.acquire(timeout=max(0, deadline - time.monotonic())):
            _LOG.warning("Background run shutdown wait exceeded its time limit")
        else:
            try:
                jobs = [
                    (job, self._job_locks.get(run_id) or self._lock)
                    for run_id, job in self._jobs.items()
                ]
            finally:
                self._lock.release()
        for (cancel, _, completed), state_lock in jobs:
            if not state_lock.acquire(timeout=max(0, deadline - time.monotonic())):
                _LOG.warning("Background run shutdown wait exceeded its time limit")
            else:
                try:
                    if not completed.is_set():
                        cancel.set()
                finally:
                    state_lock.release()
        for (_, thread, _), _ in jobs:
            thread.join(timeout=max(0, deadline - time.monotonic()))
            if thread.is_alive():
                _LOG.warning("Background run shutdown wait exceeded its time limit")
        self._retry_stop.set()
        self._retry_wakeup.set()
        retry_thread = self._retry_thread
        if retry_thread is not None:
            retry_thread.join(timeout=max(0, deadline - time.monotonic()))
            if retry_thread.is_alive():
                _LOG.warning("Background run shutdown wait exceeded its time limit")

    @staticmethod
    def _persist(run_id, status, value):
        if status == "success":
            database.finish_tool_run(run_id, status, result_text=value)
        else:
            database.finish_tool_run(run_id, status, error_text=value)

    def _supervise(self, run_id, tool_name, query, cancel, completed):
        process = None
        receiver = sender = None
        timed_out = self._context.Event()
        status, value = "failed", "Background worker failed."
        retry_in_current_thread = False
        state_lock = self._job_locks.get(run_id, self._lock)

        try:
            if not cancel.is_set() and not self._closed:
                receiver, sender = self._context.Pipe(duplex=False)
                deadline = time.monotonic() + self.limits.timeout_seconds
                process = self._context.Process(
                    target=_worker,
                    args=(sender, tool_name, query, self.limits.max_output_bytes,
                          deadline, timed_out),
                )
                process.start()
                sender.close()
                sender = None
                while True:
                    with state_lock:
                        if cancel.is_set() or self._closed:
                            break
                    remaining = deadline - time.monotonic()
                    ready = receiver.poll(max(0, min(remaining, 0.05)))
                    with state_lock:
                        if cancel.is_set() or self._closed:
                            break
                    if ready:
                        valid_payload = False
                        try:
                            payload = receiver.recv_bytes(
                                self.limits.max_output_bytes + 1
                            )
                        except (EOFError, OSError):
                            if timed_out.is_set() or time.monotonic() >= deadline:
                                status, value = (
                                    "timed_out",
                                    "Background run exceeded its time limit.",
                                )
                        else:
                            if payload and payload[:1] in (b"S", b"F", b"T"):
                                valid_payload = True
                                status = {
                                    b"S": "success",
                                    b"F": "failed",
                                    b"T": "timed_out",
                                }[payload[:1]]
                                value = payload[1:].decode(
                                    "utf-8", errors="replace"
                                )
                        with state_lock:
                            if valid_payload or (not cancel.is_set() and not self._closed):
                                completed.set()
                        break
                    with state_lock:
                        if cancel.is_set() or self._closed:
                            break
                        if remaining <= 0:
                            status, value = (
                                "timed_out",
                                "Background run exceeded its time limit.",
                            )
                            completed.set()
                            break
                        if not process.is_alive():
                            if timed_out.is_set() or time.monotonic() >= deadline:
                                status, value = (
                                    "timed_out",
                                    "Background run exceeded its time limit.",
                                )
                            completed.set()
                            break
        except Exception:
            _LOG.exception("Background worker could not be supervised")
            with state_lock:
                if not cancel.is_set() and not self._closed:
                    completed.set()
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
            with state_lock:
                if (cancel.is_set() or self._closed) and not completed.is_set():
                    status, value = "failed", "Run cancelled."
                completed.set()
            with self._lock:
                try:
                    self._persist(run_id, status, value)
                except Exception:
                    _LOG.exception("Could not persist background run outcome")
                    self._pending[run_id] = (status, value)
                    self._retry_wakeup.set()
                    retry_in_current_thread = not self._start_retry_locked()
                finally:
                    self._jobs.pop(run_id, None)
                    self._job_locks.pop(run_id, None)
            if retry_in_current_thread:
                self._retry_pending(until_empty=True)

    def _start_retry_locked(self):
        if self._retry_thread is not None and self._retry_thread.is_alive():
            return True
        try:
            thread = threading.Thread(
                target=self._retry_pending,
                name="tool-run-persistence-retry",
                daemon=True,
            )
            thread.start()
        except Exception:
            _LOG.exception("Could not start background run persistence retry")
            return False
        self._retry_thread = thread
        return True

    def _retry_pending(self, until_empty=False):
        attempts = 0
        while not self._retry_stop.is_set():
            self._retry_wakeup.wait(0.5)
            self._retry_wakeup.clear()
            if self._retry_stop.is_set():
                return
            with self._lock:
                pending = list(self._pending.items())
            for run_id, outcome in pending:
                with self._lock:
                    if self._pending.get(run_id) != outcome:
                        continue
                    try:
                        self._persist(run_id, *outcome)
                    except Exception as exc:
                        row = None
                        if isinstance(exc, ValueError):
                            try:
                                row = database.get_tool_run(run_id)
                            except Exception:
                                pass
                        if row is not None and row[3] != "running":
                            del self._pending[run_id]
                            continue
                        _LOG.warning(
                            "Could not retry persistence for run %s (%s)",
                            run_id,
                            type(exc).__name__,
                        )
                    else:
                        del self._pending[run_id]
            if until_empty:
                with self._lock:
                    if not self._pending:
                        return
                attempts += 1
                if attempts >= 3:
                    return
