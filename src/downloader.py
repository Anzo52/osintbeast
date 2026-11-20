import io
import sqlite3
import sys
import zipfile
from pathlib import Path
from pathlib import PurePosixPath, PureWindowsPath

import requests
from sqlalchemy import MetaData, Table, create_engine

import data.utils.database as database

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.utils.downloader_config import INSERTABLE_TABLES  # noqa: E402
from tool_registry import TOOL_REGISTRY, get_reviewed_repo_url  # noqa: E402

DB_FILE = database.DB_FILE


def connect_db():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def setup_database():
    database.setup_database()


def insert_data(table, data):
    """Insert into an allowlisted table with SQLAlchemy-bound values."""
    if not isinstance(table, str) or table not in INSERTABLE_TABLES:
        raise ValueError(f"Unknown database table: {table}")
    if not isinstance(data, dict) or not data:
        raise ValueError("data must be a non-empty mapping")

    engine = create_engine(f"sqlite:///{Path(DB_FILE).resolve().as_posix()}")
    try:
        metadata = MetaData()
        reflected = Table(table, metadata, autoload_with=engine)
        known_columns = {column.name for column in reflected.columns}
        if any(not isinstance(key, str) or key not in known_columns for key in data):
            raise ValueError(f"Unknown column for table {table}")
        with engine.begin() as connection:
            connection.execute(reflected.insert().values(**data))
    finally:
        engine.dispose()


def query_database(query, parameters=()):
    with connect_db() as conn:
        return conn.execute(query, parameters).fetchall()


def _validate_github_repo_url(repo_url):
    return get_reviewed_repo_url(repo_url)


def _resolve_download_directory(output_dir, tool_name):
    root = Path(output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = root / tool_name
    resolved_target = target.resolve()
    if resolved_target != target or root not in resolved_target.parents:
        raise ValueError("Tool download path escapes the designated directory.")
    return resolved_target


def download_github_repo(repo_url, output_dir):
    repo_url = _validate_github_repo_url(repo_url)
    root = Path(output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    last_error = None
    for branch in ("main", "master"):
        try:
            response = requests.get(
                f"{repo_url}/archive/refs/heads/{branch}.zip", timeout=30
            )
            response.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                for member in archive.infolist():
                    filename = member.filename
                    posix_path = PurePosixPath(filename.replace("\\", "/"))
                    windows_path = PureWindowsPath(filename)
                    if (
                        posix_path.is_absolute()
                        or windows_path.is_absolute()
                        or windows_path.drive
                        or ".." in posix_path.parts
                    ):
                        raise ValueError("Repository archive contains an unsafe path.")
                    destination = (root / filename).resolve()
                    if destination != root and root not in destination.parents:
                        raise ValueError("Repository archive contains an unsafe path.")
                archive.extractall(root)
            return str(root)
        except requests.RequestException as exc:
            last_error = exc
    raise RuntimeError(f"Unable to download repository archive from {repo_url}: {last_error}")


def download_tool(tool_name, output_dir="tools"):
    if not isinstance(tool_name, str):
        raise ValueError(f"Unsupported tool: {tool_name}")
    key = tool_name.strip().lower()
    if key not in TOOL_REGISTRY:
        raise ValueError(f"Unsupported tool: {tool_name}")
    target = _resolve_download_directory(output_dir, key)
    return download_github_repo(TOOL_REGISTRY[key]["repo"], target)
