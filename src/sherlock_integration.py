import json
import multiprocessing
import re
import time
from importlib.resources import as_file, files

SHERLOCK_REQUEST_TIMEOUT_SECONDS = 15
SHERLOCK_TIMEOUT_SECONDS = 300


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


def _search_worker(username: str, connection):
    try:
        results = _search(username)
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
        connection.send(("success", json.dumps(records, ensure_ascii=False, indent=2)))
    except TimeoutError as exc:
        connection.send(("timed_out", str(exc)))
    except Exception as exc:
        connection.send(("failed", str(exc)))
    finally:
        connection.close()


def _stop_worker(worker):
    worker.terminate()
    worker.join(timeout=1)
    if worker.is_alive():
        worker.kill()
        worker.join()


def run_sherlock(username: str, *, process_context=None, worker_target=None) -> str:
    username = validate_username(username)
    context = (
        process_context
        if process_context is not None
        else multiprocessing.get_context()
    )
    receiver, sender = context.Pipe(duplex=False)
    worker = context.Process(
        target=worker_target if worker_target is not None else _search_worker,
        args=(username, sender),
        daemon=True,
    )
    deadline = time.monotonic() + SHERLOCK_TIMEOUT_SECONDS
    try:
        worker.start()
        sender.close()
        if not receiver.poll(max(0, deadline - time.monotonic())):
            raise TimeoutError(
                f"Sherlock scan timed out (overall limit: {SHERLOCK_TIMEOUT_SECONDS} seconds)."
            )
        try:
            outcome, result = receiver.recv()
        except EOFError as exc:
            raise RuntimeError("Sherlock scan worker exited without a result.") from exc
        worker.join(timeout=max(0, deadline - time.monotonic()))
        if worker.is_alive():
            raise TimeoutError(
                f"Sherlock scan timed out (overall limit: {SHERLOCK_TIMEOUT_SECONDS} seconds)."
            )
        if outcome == "timed_out":
            raise TimeoutError(result or "Sherlock scan timed out.")
        if outcome != "success":
            raise RuntimeError(result or "Sherlock scan failed.")
        return result
    finally:
        sender.close()
        receiver.close()
        if worker.pid is not None:
            if worker.is_alive():
                _stop_worker(worker)
            else:
                worker.join()
