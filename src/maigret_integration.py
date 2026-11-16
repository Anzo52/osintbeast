import asyncio
import json
import logging
import re
from importlib.resources import as_file, files

MAIGRET_TIMEOUT_SECONDS = 300


def validate_username(username: str) -> str:
    if not isinstance(username, str):
        raise ValueError("A username is required.")
    username = username.strip()
    if not username or len(username) > 100 or not re.fullmatch(r"[A-Za-z0-9_.-]+", username):
        raise ValueError("Use a username of up to 100 letters, digits, dots, underscores or hyphens.")
    return username


async def _search(username: str):
    try:
        from maigret import search
        from maigret.sites import MaigretDatabase
    except ImportError as exc:
        raise RuntimeError(
            "Maigret is required. Install it with `python -m pip install '.[maigret]'`."
        ) from exc

    resource = files("maigret").joinpath("resources", "data.json")
    with as_file(resource) as database_path:
        sites = MaigretDatabase().load_from_path(str(database_path)).ranked_sites_dict(
            disabled=False
        )
        if not sites:
            raise RuntimeError("Maigret's site database contains no enabled username sites.")
        try:
            return await asyncio.wait_for(
                search(
                    username=username,
                    site_dict=sites,
                    logger=logging.getLogger("osintbeast.maigret"),
                    timeout=30,
                    no_progressbar=True,
                ),
                timeout=MAIGRET_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                f"Maigret search timed out (overall limit: {MAIGRET_TIMEOUT_SECONDS} seconds)."
            ) from exc


def run_maigret(username: str) -> str:
    username = validate_username(username)
    results = asyncio.run(_search(username))
    if not results:
        raise RuntimeError("Maigret returned no site results.")
    records = []
    for site, result in sorted(results.items()):
        status = result.get("status")
        if status is None:
            raise RuntimeError(f"Maigret returned a result without status for {site}.")
        records.append({
            "site": site,
            "url": result.get("url_user", ""),
            "status": str(status.status),
            "error": str(status.error) if status.error is not None else None,
        })
    return json.dumps(records, ensure_ascii=False, indent=2)
