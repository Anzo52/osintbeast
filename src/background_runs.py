"""Bounded, process-isolated execution for use by a web application."""

import hashlib
import hmac
import json
import logging
import math
import multiprocessing
import multiprocessing.connection
import os
import re
import secrets
import signal
import stat
import tempfile
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from io import StringIO
from pathlib import Path

from data.utils import database
from helix_integration import run_helix
from helix_integration import validate_username as validate_helix
from holehe_integration import run_holehe, validate_email
from maigret_integration import run_maigret
from maigret_integration import validate_username as validate_maigret
from sherlock_integration import run_sherlock
from sherlock_integration import validate_username as validate_sherlock
from tool_runs import redact_sensitive_output

_LOG = logging.getLogger(__name__)
_SHUTDOWN_TIMEOUT_SECONDS = 5
_LEASE_GRACE_SECONDS = 30
_LEASE_RENEW_INTERVAL_SECONDS = 5
_SPOOL_FILENAME = re.compile(r"run-([1-9][0-9]*)-([1-9][0-9]*)-([0-9a-f]{32})\.json\Z")
_WALL_TIME = time.time
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

    def __init__(self, run_id, startup_error=None, persistence_error=None):
        message = "Could not persist background run startup failure."
        if startup_error is not None:
            message += f" {startup_error}"
        if persistence_error is not None:
            message += f" Persistence also failed: {persistence_error}"
        super().__init__(message)
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


def _write_spooled_outcome(spool_dir, run_id, attempt, owner_token, status, value):
    if (
        not isinstance(run_id, int) or isinstance(run_id, bool)
        or not 1 <= run_id <= database.SQLITE_MAX_INTEGER
        or not isinstance(attempt, int) or isinstance(attempt, bool)
        or not 1 <= attempt <= (1 << 31) - 1
        or not isinstance(owner_token, str)
        or not re.fullmatch(r"[0-9a-f]{32}", owner_token)
        or not isinstance(status, str)
        or status not in {"success", "failed", "timed_out"}
        or not isinstance(value, str)
    ):
        raise ValueError("Invalid background outcome spool record.")
    directory = Path(spool_dir)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise OSError("Background outcome spool must be a real directory.")
    directory.chmod(0o700)
    filename = f"run-{run_id}-{attempt}-{owner_token}.json"
    destination = directory / filename
    descriptor, temporary = tempfile.mkstemp(prefix=".outcome-", dir=directory)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(
                {"run_id": run_id, "attempt": attempt, "owner_token": owner_token,
                 "status": status, "value": value},
                stream,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        if hasattr(os, "O_DIRECTORY"):
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return destination


def _worker(
    connection, tool_name, query, max_output_bytes, deadline=None,
    timed_out=None, spool_context=None,
):
    status, message = b"F", "Background worker failed."
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
        status, message = b"S", output
    except BaseException as exc:
        status = b"T" if isinstance(exc, TimeoutError) else b"F"
        if isinstance(exc, (TimeoutError, RuntimeError, OSError, ImportError, ValueError)):
            message = str(exc) or f"Worker failed ({type(exc).__name__})."
        else:
            message = f"{tool_name} failed unexpectedly ({type(exc).__name__})."
        if tool_name == "holehe":
            message = redact_sensitive_output(message, query)
        message = message.encode("utf-8")[:max_output_bytes].decode(
            "utf-8", errors="ignore"
        )
    if spool_context is not None:
        try:
            _write_spooled_outcome(*spool_context, {
                b"S": "success", b"F": "failed", b"T": "timed_out"
            }[status], message)
        except Exception:
            _LOG.exception("Could not durably spool background run outcome")
    try:
        connection.send_bytes(
            status + message.encode("utf-8")[:max_output_bytes]
        )
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


def _now_ms():
    return int(_WALL_TIME() * 1000)


def _unique_json_object(pairs):
    record = {}
    for key, value in pairs:
        if key in record:
            raise ValueError(f"Duplicate key in background outcome spool: {key}")
        record[key] = value
    return record


class BackgroundRuns:
    """A per-process service backed by durable run state and outcome files."""

    def __init__(self, limits: RunLimits = RunLimits()):
        self.limits = limits
        self._context = multiprocessing.get_context("spawn")
        self._lock = threading.Lock()
        self._idempotency_lock = threading.Lock()
        self._jobs = {}
        self._launching = set()
        self._reservations = 0
        self._spool_dir = Path(
            os.environ.get(
                "OSINTBEAST_RUN_SPOOL_DIR",
                str(Path(database.DB_FILE).resolve().parent / "osintbeast-run-spool"),
            )
        ).expanduser().absolute()
        self._spool_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self._spool_dir.is_symlink() or not self._spool_dir.is_dir():
            raise OSError("Background outcome spool must be a real directory.")
        self._spool_dir.chmod(0o700)
        if os.name == "posix" and stat.S_IMODE(self._spool_dir.stat().st_mode) != 0o700:
            raise OSError("Could not restrict background outcome spool permissions.")
        self._retry_wakeup = threading.Event()
        self._retry_stop = threading.Event()
        self._retry_thread = None
        self._closed = False
        self._recover()
        self._schedule_retry()

    def submit(
        self, tool_name: str, query: str, idempotency_key: str | None = None
    ) -> int:
        if idempotency_key is None:
            return self._submit(tool_name, query, idempotency_key)
        with self._idempotency_lock:
            return self._submit(tool_name, query, idempotency_key)

    def _submit(
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
            raise ValueError(
                "Idempotency-Key must contain 1 to 128 visible ASCII characters."
            )
        query = _TOOLS[tool_name][0](query)
        key_hash = fingerprint = None
        if idempotency_key is not None:
            key_bytes = idempotency_key.encode("ascii")
            key_hash = hashlib.sha256(key_bytes).hexdigest()
            request_hash = hmac.new(
                key_bytes,
                json.dumps(
                    [tool_name, query], ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            fingerprint = request_hash
            existing = database.get_background_run_by_key(key_hash)
            if existing is not None:
                if existing[1:] != (tool_name, fingerprint):
                    raise IdempotencyConflict(
                        "Idempotency-Key was already used for a different run."
                    )
                self._recover_run(existing[0], query=query)
                return existing[0]

        with self._lock:
            if self._closed:
                raise RuntimeError("Background run service is closed.")
            if self._active_count() >= self.limits.max_concurrent:
                raise CapacityError("Background run capacity reached.")
            self._reservations += 1
        try:
            owner_token = secrets.token_hex(16)
            lease_expires_at = _now_ms() + int(
                (self.limits.timeout_seconds + _LEASE_GRACE_SECONDS) * 1000
            )
            try:
                run_id, created, attempt = database.create_background_run(
                    tool_name,
                    "[redacted]" if tool_name == "holehe" else query,
                    key_hash,
                    fingerprint,
                    owner_token,
                    lease_expires_at,
                )
            except database.IdempotencyConflictError as exc:
                raise IdempotencyConflict(str(exc)) from exc
            if not created:
                self._recover_run(run_id, query=query)
                return run_id
            self._launch(
                run_id, tool_name, query, reserved=True, owner_token=owner_token,
                attempt=attempt, lease_expires_at=lease_expires_at,
            )
            return run_id
        finally:
            with self._lock:
                self._reservations -= 1

    def _active_count(self):
        active_jobs = sum(not job[2].is_set() for job in self._jobs.values())
        return active_jobs + len(self._launching) + self._reservations

    def _launch(
        self, run_id, tool_name, query, *, reserved=False, owner_token=None,
        attempt=None, lease_expires_at=None,
    ):
        with self._lock:
            if self._closed or run_id in self._jobs or run_id in self._launching:
                return False
            if not reserved and self._active_count() >= self.limits.max_concurrent:
                return False
            self._launching.add(run_id)
        try:
            if attempt is None:
                owner_token = secrets.token_hex(16)
                lease_expires_at = _now_ms() + int(
                    (self.limits.timeout_seconds + _LEASE_GRACE_SECONDS) * 1000
                )
                attempt = database.claim_background_run(
                    run_id, owner_token, lease_expires_at
                )
                if attempt is None:
                    return False
            cancel = threading.Event()
            completed = threading.Event()
            try:
                thread = threading.Thread(
                    target=self._supervise,
                    args=(run_id, tool_name, query, cancel, completed, owner_token,
                          attempt, lease_expires_at),
                    name=f"tool-run-{run_id}",
                    daemon=True,
                )
                with self._lock:
                    closed = self._closed
                    if not closed:
                        self._jobs[run_id] = (
                            cancel, thread, completed, owner_token, attempt
                        )
                if closed:
                    database.cancel_background_run(run_id)
                    return False
                thread.start()
            except BaseException as startup_error:
                with self._lock:
                    self._jobs.pop(run_id, None)
                self._record_startup_failure(
                    run_id, owner_token, attempt, startup_error
                )
            return True
        except Exception:
            self._schedule_retry()
            raise
        finally:
            with self._lock:
                self._launching.discard(run_id)

    def _record_startup_failure(self, run_id, owner_token, attempt, startup_error):
        status, value = "failed", "Could not start background worker."
        try:
            path = _write_spooled_outcome(
                self._spool_dir, run_id, attempt, owner_token, status, value
            )
            persisted = database.finish_background_run(
                run_id, owner_token, attempt, status, error_text=value
            )
            if persisted:
                self._persist_spool_file(path)
            else:
                self._schedule_retry()
        except Exception as persistence_error:
            _LOG.exception("Could not persist background run startup failure")
            self._schedule_retry()
            if isinstance(startup_error, Exception):
                raise StartupPersistenceError(
                    run_id, startup_error, persistence_error
                ) from startup_error
        if isinstance(startup_error, BaseException) and not isinstance(
            startup_error, Exception
        ):
            try:
                startup_error.run_id = run_id
            except (AttributeError, TypeError):
                pass
        raise startup_error

    def _schedule_retry(self):
        with self._lock:
            self._retry_wakeup.set()
            if self._retry_thread is not None and self._retry_thread.is_alive():
                return
            try:
                thread = threading.Thread(
                    target=self._retry_pending,
                    name="tool-run-persistence-retry",
                    daemon=True,
                )
                self._retry_thread = thread
                thread.start()
            except Exception:
                self._retry_thread = None
                _LOG.exception("Could not start background recovery worker")

    def _read_spool(self, path):
        match = _SPOOL_FILENAME.fullmatch(path.name)
        if match is None:
            raise ValueError(f"Invalid background outcome filename: {path.name}")
        filename_run_id, filename_attempt = int(match[1]), int(match[2])
        filename_owner = match[3]
        directory_fd = os.open(
            self._spool_dir,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            descriptor = os.open(
                path.name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_fd,
            )
            try:
                details = os.fstat(descriptor)
                path_details = os.stat(
                    path.name, dir_fd=directory_fd, follow_symlinks=False
                )
                if not stat.S_ISREG(details.st_mode):
                    raise ValueError("Background outcome spool entry is not a regular file.")
                if (
                    not stat.S_ISREG(path_details.st_mode)
                    or (details.st_dev, details.st_ino)
                    != (path_details.st_dev, path_details.st_ino)
                ):
                    raise ValueError("Background outcome spool entry changed while opening.")
                if os.name == "posix" and stat.S_IMODE(details.st_mode) & 0o077:
                    raise ValueError("Background outcome spool file permissions are too broad.")
                if details.st_size > self.limits.max_output_bytes * 6 + 4096:
                    raise ValueError("Background outcome spool file is too large.")
                with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                    descriptor = -1
                    record = json.load(stream, object_pairs_hook=_unique_json_object)
                    identity = (details.st_dev, details.st_ino)
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
        finally:
            os.close(directory_fd)
        if (
            not isinstance(record, dict)
            or set(record) != {"run_id", "attempt", "owner_token", "status", "value"}
            or isinstance(record.get("run_id"), bool)
            or not isinstance(record.get("run_id"), int)
            or isinstance(record.get("attempt"), bool)
            or not isinstance(record.get("attempt"), int)
            or not isinstance(record.get("owner_token"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", record["owner_token"])
            or not isinstance(record.get("value"), str)
            or not isinstance(record.get("status"), str)
            or record["status"] not in {"success", "failed", "timed_out"}
            or not 1 <= record["run_id"] <= database.SQLITE_MAX_INTEGER
            or not 1 <= record["attempt"] <= (1 << 31) - 1
            or len(record["value"].encode("utf-8")) > self.limits.max_output_bytes
            or (
                record["run_id"],
                record["attempt"],
                record["owner_token"],
            ) != (filename_run_id, filename_attempt, filename_owner)
        ):
            raise ValueError("Invalid or mismatched background outcome spool record.")
        return record, identity

    def _persist_spool_file(self, path):
        try:
            record, identity = self._read_spool(path)
        except FileNotFoundError:
            return True
        persisted = database.finish_background_run(
            record["run_id"],
            record["owner_token"],
            record["attempt"],
            record["status"],
            result_text=record["value"] if record["status"] == "success" else None,
            error_text=record["value"] if record["status"] != "success" else None,
        )
        if not persisted:
            state = database.get_background_run_state(record["run_id"])
            if state is None or state[0] not in {
                "success", "failed", "timed_out", "cancelled",
            }:
                return False
            row = database.get_tool_run(record["run_id"])
            if state[0] == "cancelled":
                if (
                    state[1] != record["attempt"] or row is None
                    or row[3:6] != ("failed", None, "Run cancelled.")
                ):
                    return False
            elif row is None or (
                state[0],
                row[4] if state[0] == "success" else row[5],
            ) != (record["status"], record["value"]):
                return False
        directory_fd = os.open(
            self._spool_dir,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            try:
                current = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                return True
            if not stat.S_ISREG(current.st_mode):
                raise ValueError("Refusing to delete a non-regular spool entry.")
            if (current.st_dev, current.st_ino) != identity:
                raise ValueError("Refusing to delete a replaced spool outcome.")
            os.unlink(path.name, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)
        return True

    def _process_spooled_outcomes(self, run_id=None):
        paths = sorted(self._spool_dir.glob("run-*.json"))
        persistence_error = None
        for path in paths:
            if run_id is not None and not path.name.startswith(f"run-{run_id}-"):
                continue
            try:
                self._persist_spool_file(path)
            except ValueError:
                _LOG.warning(
                    "Rejected invalid background outcome spool %s", path.name,
                    exc_info=True,
                )
            except Exception as exc:
                _LOG.warning(
                    "Could not retry persistence for background outcome %s", path.name,
                    exc_info=True,
                )
                persistence_error = persistence_error or exc
        return persistence_error is not None

    def _recover_run(self, run_id, *, query=None):
        self._process_spooled_outcomes(run_id)
        state = database.get_background_run_state(run_id)
        if state is None:
            return
        if state[0] == "queued":
            row = database.get_tool_run(run_id)
            if row is not None and query is not None:
                self._launch(run_id, row[1], query)
        elif (
            state[0] == "running" and state[3] is not None
            and state[3] <= _now_ms()
        ):
            self._recover_expired_run(run_id, state)

    def _recover_expired_run(self, run_id, state):
        if self._process_spooled_outcomes(run_id):
            return
        current = database.get_background_run_state(run_id)
        if current is None or current[0] != "running":
            return
        if current[3] is not None and current[3] <= _now_ms():
            row = database.get_tool_run(run_id)
            if row is None:
                return
            can_retry = row[1] != "holehe" and row[2] != "[redacted]"
            changed = database.recover_expired_background_run(
                run_id, current[2], current[1], _now_ms(), can_retry
            )
            if changed and can_retry:
                if not self._launch(run_id, row[1], row[2]):
                    self._schedule_retry()

    def _recover(self):
        self._process_spooled_outcomes()
        for row in database.list_background_runs_for_recovery():
            run_id, state, attempt, owner_token, _lease, tool_name, query = row
            if state == "queued":
                if query == "[redacted]":
                    database.abandon_queued_background_run(run_id, _now_ms())
                    continue
                if not self._launch(run_id, tool_name, query):
                    self._schedule_retry()
            else:
                self._recover_expired_run(
                    run_id, (state, attempt, owner_token, _lease)
                )

    def status(self, run_id: int):
        try:
            self._recover_run(run_id)
            row = database.get_tool_run(run_id, redact_sensitive=True)
        except Exception as exc:
            raise RuntimeError("Could not persist background run outcome.") from exc
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
            if job is not None and job[2].is_set():
                return False
        if self._process_spooled_outcomes(run_id):
            return False
        if not database.cancel_background_run(run_id):
            return False
        with self._lock:
            job = self._jobs.get(run_id)
            if job is not None:
                job[0].set()
                job[2].set()
        return True

    def close(self):
        deadline = time.monotonic() + _SHUTDOWN_TIMEOUT_SECONDS
        with self._lock:
            self._closed = True
            jobs = list(self._jobs.items())
        for run_id, job in jobs:
            try:
                cancelled = (
                    not job[2].is_set() and database.cancel_background_run(run_id)
                )
            except Exception:
                cancelled = False
                _LOG.exception(
                    "Could not persist cancellation during background shutdown"
                )
            with self._lock:
                job[0].set()
                if cancelled:
                    job[2].set()
        for _, job in jobs:
            thread = job[1]
            if thread.ident is not None:
                thread.join(timeout=max(0, deadline - time.monotonic()))
            if thread.is_alive():
                _LOG.warning("Background run shutdown wait exceeded its time limit")
        self._retry_stop.set()
        self._retry_wakeup.set()
        retry_thread = self._retry_thread
        if retry_thread is not None and retry_thread is not threading.current_thread():
            retry_thread.join(timeout=max(0, deadline - time.monotonic()))
            if retry_thread.is_alive():
                _LOG.warning("Background run shutdown wait exceeded its time limit")

    def _supervise(
        self, run_id, tool_name, query, cancel, completed,
        owner_token=None, attempt=None, lease_expires_at=None,
    ):
        process = None
        receiver = sender = None
        timed_out = self._context.Event()
        status, value = "failed", "Background worker failed."
        payload_received = False
        ownership_lost = False
        if owner_token is None:
            owner_token = secrets.token_hex(16)
            database.ensure_background_run(run_id)
            lease_expires_at = _now_ms() + int(
                (self.limits.timeout_seconds + _LEASE_GRACE_SECONDS) * 1000
            )
            attempt = database.claim_background_run(
                run_id, owner_token, lease_expires_at,
            )
            if attempt is None:
                return
        renew_interval = min(
            _LEASE_RENEW_INTERVAL_SECONDS,
            max(0.1, self.limits.timeout_seconds / 3),
        )
        next_renewal = time.monotonic() + renew_interval
        try:
            if not cancel.is_set() and not self._closed:
                receiver, sender = self._context.Pipe(duplex=False)
                deadline = time.monotonic() + self.limits.timeout_seconds
                process = self._context.Process(
                    target=_worker,
                    args=(
                        sender, tool_name, query, self.limits.max_output_bytes,
                        deadline, timed_out,
                        (str(self._spool_dir), run_id, attempt, owner_token),
                    ),
                )
                process.start()
                sender.close()
                sender = None
                next_state_check = 0.0
                while True:
                    if cancel.is_set() or self._closed:
                        break
                    now = time.monotonic()
                    if now >= next_state_check:
                        try:
                            state = database.get_background_run_state(run_id)
                            if state is not None and state[0] not in {"running"}:
                                completed.set()
                                break
                        except Exception:
                            _LOG.debug("Could not check background run state", exc_info=True)
                        next_state_check = now + 0.25
                    if now >= next_renewal:
                        new_expiry = _now_ms() + int(
                            (self.limits.timeout_seconds + _LEASE_GRACE_SECONDS) * 1000
                        )
                        try:
                            renewed = database.renew_background_run(
                                run_id, owner_token, attempt, _now_ms(), new_expiry
                            )
                        except Exception:
                            renewed = False
                            _LOG.warning(
                                "Could not renew background run lease for %s",
                                run_id,
                                exc_info=True,
                            )
                        if renewed:
                            lease_expires_at = new_expiry
                            next_renewal = now + renew_interval
                        elif _now_ms() >= lease_expires_at:
                            ownership_lost = True
                            cancel.set()
                            break
                        else:
                            next_renewal = now + min(1.0, renew_interval)
                    remaining = deadline - now
                    ready = receiver.poll(max(0, min(remaining, 0.05)))
                    if cancel.is_set() or self._closed:
                        break
                    if ready:
                        try:
                            received = receiver.recv_bytes(
                                self.limits.max_output_bytes + 1
                            )
                        except (EOFError, OSError):
                            if timed_out.is_set() or time.monotonic() >= deadline:
                                status, value = (
                                    "timed_out",
                                    "Background run exceeded its time limit.",
                                )
                        else:
                            if received and received[:1] in (b"S", b"F", b"T"):
                                payload_received = True
                                status = {
                                    b"S": "success", b"F": "failed", b"T": "timed_out"
                                }[received[:1]]
                                value = received[1:].decode("utf-8", errors="replace")
                        if payload_received:
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
                    if remaining <= 0:
                        status, value = (
                            "timed_out",
                            "Background run exceeded its time limit.",
                        )
                        completed.set()
                        break
        except Exception:
            _LOG.exception("Background worker could not be supervised")
            if not completed.is_set():
                status, value = "failed", "Background worker failed."
        finally:
            if process is not None:
                try:
                    _stop_process(process)
                except Exception:
                    _LOG.exception("Could not stop background worker")
                    if not payload_received:
                        status, value = "failed", "Could not stop background worker."
            if receiver is not None:
                receiver.close()
            if sender is not None:
                sender.close()
            if (
                not ownership_lost and (cancel.is_set() or self._closed)
                and not completed.is_set()
            ):
                status, value = "failed", "Run cancelled."
            completed.set()
            if ownership_lost:
                _LOG.warning(
                    "Background run %s lost ownership before completion", run_id
                )
                with self._lock:
                    self._jobs.pop(run_id, None)
                return
            try:
                path = self._spool_dir / f"run-{run_id}-{attempt}-{owner_token}.json"
                if not path.exists():
                    path = _write_spooled_outcome(
                        self._spool_dir, run_id, attempt, owner_token, status, value
                    )
                if not self._persist_spool_file(path):
                    self._schedule_retry()
            except Exception:
                _LOG.exception("Could not persist background run outcome")
                self._schedule_retry()
            finally:
                with self._lock:
                    self._jobs.pop(run_id, None)
                self._schedule_retry()

    def _retry_pending(self):
        retry_delay = 0.5
        wait_delay = retry_delay
        had_pending = False
        while not self._retry_stop.is_set():
            self._retry_wakeup.wait(wait_delay)
            self._retry_wakeup.clear()
            if self._retry_stop.is_set():
                return
            try:
                self._recover()
                pending = any(self._spool_dir.glob("run-*.json"))
                pending = pending or bool(database.list_background_runs_for_recovery())
            except Exception as exc:
                _LOG.warning(
                    "Background run recovery failed (%s)", type(exc).__name__
                )
                pending = True
            if pending:
                retry_delay = (
                    min(retry_delay * 2, 30) if had_pending else 0.5
                )
                wait_delay = retry_delay
                had_pending = True
            else:
                retry_delay = 0.5
                wait_delay = 5
                had_pending = False
