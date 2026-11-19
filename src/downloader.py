import io
import os
import sqlite3
import sys
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import requests
from sqlalchemy import MetaData, Table, create_engine

import data.utils.database as database

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data.utils.downloader_config import INSERTABLE_TABLES, SCHEMA, TOOL_REPOSITORIES

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
    parsed = urlparse(repo_url)
    parts = parsed.path.strip("/").split("/")
    if parsed.scheme != "https" or parsed.hostname != "github.com" or len(parts) != 2:
        raise ValueError("Only HTTPS GitHub owner/repository URLs are supported.")
    if any(not part or part in {".", ".."} for part in parts):
        raise ValueError("Invalid GitHub repository URL.")
    return f"https://github.com/{parts[0]}/{parts[1]}"


def download_github_repo(repo_url, output_dir):
    repo_url = _validate_github_repo_url(repo_url)
    os.makedirs(output_dir, exist_ok=True)
    last_error = None
    for branch in ("main", "master"):
        try:
            response = requests.get(
                f"{repo_url}/archive/refs/heads/{branch}.zip", timeout=30
            )
            response.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                root = Path(output_dir).resolve()
                for member in archive.infolist():
                    destination = (root / member.filename).resolve()
                    if destination != root and root not in destination.parents:
                        raise ValueError("Repository archive contains an unsafe path.")
                archive.extractall(root)
            return str(root)
        except requests.RequestException as exc:
            last_error = exc
    raise RuntimeError(f"Unable to download repository archive from {repo_url}: {last_error}")


def download_tool(tool_name, output_dir="tools"):
    key = tool_name.strip().lower()
    if key not in TOOL_REPOSITORIES:
        raise ValueError(f"Unsupported tool: {tool_name}")
    return download_github_repo(TOOL_REPOSITORIES[key], os.path.join(output_dir, key))
