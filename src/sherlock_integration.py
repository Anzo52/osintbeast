import json
import multiprocessing
import re
import threading
import time
from importlib.resources import as_file, files

from tool_runs import redact_sensitive_data

SHERLOCK_REQUEST_TIMEOUT_SECONDS = 15
SHERLOCK_TIMEOUT_SECONDS = 300
MAX_CONCURRENT_SHERLOCK_WORKERS = 2
# Include the pipe frame header within POSIX's minimum atomic write size.
_WORKER_MESSAGE_CHUNK_SIZE = 500
_WORKER_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_SHERLOCK_WORKERS)


def validate_username(username: str) -> str:
    if not isinstance(username, str):
        raise ValueError("A username is required.")
    username = username.strip()
    if not username or len(username) > 100 or not re.fullmatch(r"[A-Za-z0-9_.-]+", username):
        raise ValueError("Use a username of up to 100 letters, digits, dots, underscores or hyphens.")
    return username


def _search(username: str):
    try:
        from sherlock_project.notify import QueryNotify
        from sherlock_project.sherlock import sherlock
        from sherlock_project.sites import SitesInformation
    except ImportError as exc:
        raise RuntimeError(
            "Sherlock is required. Install it with `python -m pip install -e '.[sherlock]'`."
        ) from exc

    resource = files("sherlock_project").joinpath("resources", "data.json")
    with as_file(resource) as data_path:
        sites = SitesInformation(data_file_path=str(data_path))
        sites.remove_nsfw_sites()
        if not sites.sites:
            raise RuntimeError("Sherlock's site database contains no supported sites.")
        return sherlock(
            username,
            site_data=sites.sites,
            query_notify=QueryNotify(),
            timeout=SHERLOCK_REQUEST_TIMEOUT_SECONDS,
        )


def _send_worker_message(connection, outcome, message):
    payload = json.dumps({"outcome": outcome, "message": message}).encode("utf-8")
    for offset in range(0, len(payload), _WORKER_MESSAGE_CHUNK_SIZE):
        connection.send_bytes(payload[offset:offset + _WORKER_MESSAGE_CHUNK_SIZE])


def _search_worker(username: str, connection, search_function=None):
    try:
        search = search_function if search_function is not None else _search
        results = search(username)
        if not results:
            raise RuntimeError("Sherlock returned no site results.")

        records = []
        for site, result in sorted(results.items()):
            status = result.get("status")
            if status is None:
                raise RuntimeError(f"Sherlock returned a result without status for {site}.")
            records.append({
                "site": site,
                "url": result.get("url_user", ""),
                "status": str(status.status),
                "http_status": result.get("http_status"),
                "error": status.context,
            })
        _send_worker_message(connection, "success", records)
    except TimeoutError as exc:
        _send_worker_message(connection, "timed_out", str(exc))
    except Exception as exc:
        message = str(exc)
        if isinstance(exc, (TypeError, ValueError)):
            message = "Sherlock returned results that could not be serialized."
        _send_worker_message(connection, "failed", message)
    finally:
        connection.close()


def _stop_worker(worker):
    worker.terminate()
    worker.join(timeout=1)
    if worker.is_alive():
        worker.kill()
        worker.join()


def run_sherlock(username: str, *, process_context=None, search_function=None) -> str:
    username = validate_username(username)
    if not _WORKER_SLOTS.acquire(blocking=False):
        raise RuntimeError("Sherlock is busy; try again later.")

    deadline = time.monotonic() + SHERLOCK_TIMEOUT_SECONDS
    receiver = sender = worker = None
    try:
        context = (
            process_context
            if process_context is not None
            else multiprocessing.get_context("spawn")
        )
        receiver, sender = context.Pipe(duplex=False)
        worker = context.Process(
            target=_search_worker,
            args=(username, sender, search_function),
            daemon=True,
        )
        worker.start()
        sender.close()
        payload = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not receiver.poll(remaining):
                raise TimeoutError(
                    f"Sherlock scan timed out (overall limit: {SHERLOCK_TIMEOUT_SECONDS} seconds)."
                )
            try:
                payload.extend(receiver.recv_bytes(_WORKER_MESSAGE_CHUNK_SIZE))
            except EOFError:
                break
        worker.join(timeout=max(0, deadline - time.monotonic()))
        if worker.is_alive():
            raise TimeoutError(
                f"Sherlock scan timed out (overall limit: {SHERLOCK_TIMEOUT_SECONDS} seconds)."
            )
        try:
            response = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Sherlock scan worker returned an invalid result.") from exc
        if response["outcome"] == "timed_out":
            raise TimeoutError(response["message"] or "Sherlock scan timed out.")
        if response["outcome"] != "success":
            raise RuntimeError(response["message"] or "Sherlock scan failed.")
        try:
            return json.dumps(
                redact_sensitive_data(response["message"], username),
                ensure_ascii=False,
                indent=2,
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Sherlock returned results that could not be serialized.") from exc
    finally:
        if sender is not None:
            sender.close()
        if receiver is not None:
            receiver.close()
        if worker is not None and worker.pid is not None:
            if worker.is_alive():
                _stop_worker(worker)
            else:
                worker.join()
        _WORKER_SLOTS.release()
