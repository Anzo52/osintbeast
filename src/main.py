import sqlite3
import subprocess
import zipfile
import zlib

from simple_term_menu import TerminalMenu

from checkers import _run_email_check_menu, _run_username_check_menu
from backend_services import get_recent_tool_runs, get_saved_tool_results
from data.utils.init_db import init_db
from menu_utils import _choose_option, _download_tool_menu, _install_tool_menu
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


def query_menu():
    choice = _choose_option(
        [
            "Recent Tool Runs",
            "Saved Tool Results",
            "Back",
        ],
        "Choose database view",
    )
    try:
        if choice == 0:
            result = get_recent_tool_runs()
        elif choice == 1:
            result = get_saved_tool_results()
        else:
            return
    except sqlite3.Error as exc:
        print(f"Database query failed: {exc}")
        return
    print(f"Query result: {result}")


if __name__ == "__main__":
    while True:
        main_menu()
