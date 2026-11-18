import sqlite3
import subprocess
import zipfile
import zlib

from simple_term_menu import TerminalMenu

from database import setup_database, query_database
from downloader import download_tool
from helix_integration import run_helix, validate_username as validate_helix_username
from installer import install_tool
from holehe_integration import run_holehe, validate_email
from maigret_integration import run_maigret, validate_username
from tool_registry import get_supported_tools, is_tool_installed
from tool_runs import execute_tool_run


def main_menu():
    options = [
        "Initialize Database",
        "List Supported Tools",
        "Download Tool",
        "Install Tool",
        "Run Holehe Check",
        "Query Database",
        "Run Maigret Check",
        "Run Helix Check",
        "Exit",
    ]
    terminal_menu = TerminalMenu(options, title="OSINT Beast Main Menu")
    choice = terminal_menu.show()

    if choice == 0:
        init_db()
    elif choice == 1:
        list_supported_tools()
    elif choice == 2:
        tool_name = input("Enter the name of the tool to download: ").strip()
        download(tool_name)
    elif choice == 3:
        tool_name = input("Enter the name of the tool to install: ").strip()
        install(tool_name)
    elif choice == 4:
        email = input("Enter the email to check: ").strip()
        run_holehe_check(email)
    elif choice == 5:
        query_str = input("Enter your query: ").strip()
        query(query_str)
    elif choice == 6:
        username = input("Enter the username to check: ").strip()
        run_maigret_check(username)
    elif choice == 7:
        username = input("Enter the username to check: ").strip()
        run_helix_check(username)
    elif choice == 8:
        raise SystemExit(0)


def init_db():
    try:
        setup_database()
    except (OSError, sqlite3.Error, UnicodeError) as exc:
        print(f"Database initialization failed: {exc}")
        return
    print("Database initialized successfully.")


def list_supported_tools():
    for tool_name in get_supported_tools():
        status = "installed" if is_tool_installed(tool_name) else "not installed"
        print(f"- {tool_name}: {status}")


def download(tool_name):
    try:
        target_dir = download_tool(tool_name, output_dir="tools")
    except (
        ValueError, RuntimeError, OSError, zipfile.BadZipFile,
        EOFError, NotImplementedError, zlib.error,
    ) as exc:
        print(f"Download failed: {exc}")
        return
    print(f"{tool_name} downloaded successfully to {target_dir}.")


def install(tool_name):
    tool_path = input("Enter the local tool path or press Enter to use the default download directory: ").strip()
    try:
        install_tool(tool_name, tool_path or f"tools/{tool_name.strip().lower()}")
    except (ValueError, OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"Installation failed: {exc}")
        return
    print(f"{tool_name} installed successfully.")


def _run_check(tool_name, query_text, validator, runner):
    try:
        query_text = validator(query_text)
    except ValueError as exc:
        print(f"Input error: {exc}")
        return
    try:
        status, value = execute_tool_run(tool_name, query_text, runner)
    except sqlite3.Error as exc:
        print(f"Could not record {tool_name} run: {exc}. Initialize the database and check its permissions.")
        return
    if status == "success":
        print(value)
        print(f"{tool_name.capitalize()} check saved to the database.")
    elif status == "timed_out":
        print(f"{tool_name.capitalize()} timed out: {value}")
    else:
        print(f"{tool_name.capitalize()} could not run: {value}")


def run_holehe_check(email):
    _run_check("holehe", email, validate_email, lambda value: run_holehe(value)["output"])


def run_maigret_check(username):
    _run_check("maigret", username, validate_username, run_maigret)


def run_helix_check(username):
    _run_check("helix", username, validate_helix_username, run_helix)


def query(query_text):
    try:
        result = query_database(query_text)
    except sqlite3.Error as exc:
        print(f"Database query failed: {exc}")
        return
    print(f"Query result: {result}")


if __name__ == "__main__":
    while True:
        main_menu()
