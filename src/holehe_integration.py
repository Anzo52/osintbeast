import sqlite3
import subprocess
import sys
from pathlib import Path

DB_FILE = Path(__file__).resolve().parent.parent / "osintbeast.db"


def validate_email(email: str) -> str:
    email = (email or "").strip()
    if not email or "@" not in email:
        raise ValueError("A valid email address is required.")
    return email


def run_holehe(email: str):
    email = validate_email(email)

    cmd = [sys.executable, "-m", "holehe", email]
    result = subprocess.run(cmd, capture_output=True, text=True)

    output = (result.stdout or "").strip()
    if not output:
        output = (result.stderr or "").strip()

    return {
        "returncode": result.returncode,
        "output": output,
    }


def save_tool_result(tool_name: str, query_text: str, result_text: str):
    conn = sqlite3.connect(DB_FILE)
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
    conn.close()
