import os
import sqlite3
import subprocess
import zipfile
import zlib

from simple_term_menu import TerminalMenu

from checkers import _run_email_check_menu, _run_username_check_menu
from data.utils.init_db import init_db
from menu_utils import _choose_option, _download_tool_menu, _install_tool_menu
from data.utils.database import (
    export_tool_runs_json,
    get_tool_run,
    list_tool_runs,
    query_database,
)
from downloader import download_tool
from helix_integration import run_helix, validate_username as validate_helix_username
from installer import install_tool
from holehe_integration import run_holehe, validate_email
from maigret_integration import run_maigret, validate_username
from sherlock_integration import run_sherlock, validate_username as validate_sherlock_username
from tool_registry import get_supported_tools, is_tool_installed
from tool_runs import execute_tool_run


def main_menu():
    options = menu_ops()
    terminal_menu = TerminalMenu(options, title="OSINT Beast Main Menu")
    choice = terminal_menu.show()

    actions = menu_actions()
    action = actions.get(choice) if isinstance(choice, int) else None
    if action:
        action()

def menu_actions():
    actions = {
        0: init_db,
        1: list_supported_tools,
        2: lambda: _download_tool_menu(download),
        3: lambda: _install_tool_menu(install),
        4: lambda: _run_email_check_menu(run_holehe_check),
        5: query_menu,
        6: lambda: _run_username_check_menu(run_maigret_check),
        7: lambda: _run_username_check_menu(run_helix_check),
        8: lambda: _run_username_check_menu(run_sherlock_check),
        9: lambda: (_ for _ in ()).throw(SystemExit(0)),
    }
    return actions

def menu_ops():
    options = [
        "Initialize Database",
        "List Supported Tools",
        "Download Tool",
        "Install Tool",
        "Run Holehe Check",
        "Query Database",
        "Run Maigret Check",
        "Run Helix Check",
        "Run Sherlock Check",
        "Exit",
    ]
    return options


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


def install(tool_name, tool_path=None):
    tool_path = tool_path or f"tools/{tool_name.strip().lower()}"
    try:
        install_tool(tool_name, tool_path)
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


def run_sherlock_check(username):
    _run_check("sherlock", username, validate_sherlock_username, run_sherlock)


def query(query_text):
    try:
        result = query_database(query_text)
    except sqlite3.Error as exc:
        print(f"Database query failed: {exc}")
        return
    print(f"Query result: {result}")


def query_menu():
    choice = _choose_option(
        [
            "Run History",
            "Saved Tool Results",
            "Enter a Custom SQL Query",
            "Back",
        ],
        "Choose database view",
    )
    if choice == 0:
        run_history_menu()
    elif choice == 1:
        query(
            "SELECT resultID, toolName, queryText, resultText, createdAt "
            "FROM ToolResults ORDER BY resultID DESC LIMIT 20"
        )
    elif choice == 2:
        query(input("Enter your SQL query: ").strip())


def show_run_history(tool_name=None, status=None, offset=0):
    try:
        runs = list_tool_runs(
            limit=20, offset=offset, tool_name=tool_name, status=status
        )
    except sqlite3.Error as exc:
        print(f"Run history query failed: {exc}")
        return
    if not runs:
        print("No matching runs.")
        return
    for run_id, tool, query_text, run_status, started, finished in runs:
        summary = query_text[:80] + ("…" if len(query_text) > 80 else "")
        print(
            f"Run {run_id} | {tool} | input={summary!r} | {run_status} | "
            f"started={started} | finished={finished or '—'}"
        )


def inspect_tool_run(run_id):
    try:
        run = get_tool_run(run_id)
    except (ValueError, sqlite3.Error) as exc:
        print(f"Could not inspect run: {exc}")
        return
    if run is None:
        print(f"Run {run_id} was not found.")
        return
    print(
        f"Run {run[0]} | {run[1]} | input={run[2]!r} | {run[3]} | "
        f"started={run[4]} | finished={run[5] or '—'}"
    )
    print(f"Result: {run[6]!r}" if run[6] else "Result: (empty)")
    if run[7] is not None:
        print(f"Error: {run[7]!r}")


def _parse_run_ids(value):
    try:
        run_ids = [int(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError:
        print("Enter run IDs separated by commas.")
        return None
    if not run_ids:
        print("Select at least one run ID.")
        return None
    return run_ids


def export_selected_runs():
    run_ids = _parse_run_ids(input("Run IDs to export (comma-separated): "))
    if run_ids is None:
        return
    output_path = input("New JSON file path: ").strip()
    if not output_path:
        print("Export cancelled.")
        return
    include_sensitive = input(
        "Include sensitive input, result, and error text? [y/N]: "
    ).strip().casefold() in {"y", "yes"}
    try:
        content = export_tool_runs_json(run_ids, include_sensitive=include_sensitive)
        with open(
            output_path,
            "x",
            encoding="utf-8",
            opener=lambda path, flags: os.open(path, flags, 0o600),
        ) as export_file:
            export_file.write(content + "\n")
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"Could not export runs: {exc}")
        return
    print(f"Exported {len(run_ids)} run(s) to {output_path}.")


def run_history_menu():
    tool_name = None
    status = None
    offset = 0
    show_run_history(tool_name, status, offset)
    options = [
        "Refresh history",
        "Filter by tool/status",
        "Inspect a run result",
        "Next page",
        "Previous page",
        "Export selected runs as JSON",
        "Back",
    ]
    while True:
        choice = _choose_option(options, "Run History")
        if choice is None or choice == 6:
            return
        if choice == 0:
            show_run_history(tool_name, status, offset)
        elif choice == 1:
            tool_value = input("Tool name (blank for any): ").strip()
            status_value = input(
                "Status (running, success, failed, timed_out; blank for any): "
            ).strip()
            tool_name = tool_value or None
            status = status_value or None
            offset = 0
            try:
                list_tool_runs(tool_name=tool_name, status=status)
            except (ValueError, sqlite3.Error) as exc:
                print(f"Invalid history filter: {exc}")
                tool_name = status = None
            show_run_history(tool_name, status, offset)
        elif choice == 2:
            value = input("Run ID to inspect: ").strip()
            try:
                inspect_tool_run(int(value))
            except ValueError:
                print("Enter a valid run ID.")
        elif choice == 3:
            offset += 20
            show_run_history(tool_name, status, offset)
        elif choice == 4:
            offset = max(0, offset - 20)
            show_run_history(tool_name, status, offset)
        elif choice == 5:
            export_selected_runs()


if __name__ == "__main__":
    while True:
        main_menu()
