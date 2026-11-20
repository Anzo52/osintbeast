import json
import re
from importlib.resources import as_file, files

from tool_runs import redact_sensitive_data

SHERLOCK_REQUEST_TIMEOUT_SECONDS = 15


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


def run_sherlock(username: str) -> str:
    username = validate_username(username)
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
    try:
        return json.dumps(
            redact_sensitive_data(records, username), ensure_ascii=False, indent=2
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Sherlock returned results that could not be serialized.") from exc
