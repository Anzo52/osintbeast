import re
import sqlite3
import subprocess
import sys
from pathlib import Path

DB_FILE = Path(__file__).resolve().parent.parent / "osintbeast.db"

_LOCAL_PART_RE = re.compile(
    r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*\Z"
)
_DOMAIN_LABEL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")


def validate_email(email: str) -> str:
    """Validate a practical Internet email address before invoking Holehe.

    This checks syntax only; it does not verify that the mailbox exists or that
    the caller is authorized to use it.
    """
    if not isinstance(email, str):
        raise ValueError("A valid email address is required.")

    email = email.strip()
    if not email or len(email) > 254 or any(char.isspace() for char in email):
        raise ValueError("A valid email address is required.")
    if email.count("@") != 1:
        raise ValueError("A valid email address is required.")

    local_part, domain = email.rsplit("@", 1)
    if len(local_part) > 64 or not _LOCAL_PART_RE.fullmatch(local_part):
        raise ValueError("A valid email address is required.")

    try:
        ascii_domain = domain.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("A valid email address is required.") from exc

    labels = ascii_domain.split(".")
    if (
        len(ascii_domain) > 253
        or len(labels) < 2
        or any(not _DOMAIN_LABEL_RE.fullmatch(label) for label in labels)
    ):
        raise ValueError("A valid email address is required.")

    return email


def run_holehe(email: str):
    email = validate_email(email)

    # Pass arguments directly (never through a shell): the validated address is
    # one argv item, so shell metacharacters cannot become commands. The module
    # and executable are fixed by the application.
    cmd = [sys.executable, "-m", "holehe", email]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
        timeout=300,
    )

    output = (result.stdout or "").strip()
    if not output:
        output = (result.stderr or "").strip()

    return {
        "returncode": result.returncode,
        "output": output,
    }


def save_tool_result(tool_name: str, query_text: str, result_text: str):
    conn = sqlite3.connect(DB_FILE)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ToolResults (
                resultID INTEGER PRIMARY KEY AUTOINCREMENT,
                toolName TEXT NOT NULL,
                queryText TEXT NOT NULL,
                resultText TEXT,
                createdAt TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            INSERT INTO ToolResults (toolName, queryText, resultText)
            VALUES (?, ?, ?)
            """,
            (tool_name, query_text, result_text),
        )
        conn.commit()
    finally:
        conn.close()
