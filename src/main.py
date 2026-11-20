import argparse
import sqlite3
import subprocess
import sys
import zipfile
import zlib

from simple_term_menu import TerminalMenu

from checkers import _run_email_check_menu, _run_username_check_menu
from data.utils.init_db import init_db
from menu_utils import _choose_option, _download_tool_menu, _install_tool_menu
from data.utils.database import query_database
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
        return 1
    print(f"{tool_name} downloaded successfully to {target_dir}.")
    return 0


def install(tool_name, tool_path=None):
    tool_path = tool_path or f"tools/{tool_name.strip().lower()}"
    try:
        install_tool(tool_name, tool_path)
    except (ValueError, OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"Installation failed: {exc}")
        return 1
    print(f"{tool_name} installed successfully.")
    return 0


def _run_check(tool_name, query_text, validator, runner):
    try:
        query_text = validator(query_text)
    except ValueError as exc:
        print(f"Input error: {exc}")
        return 2
    try:
        status, value = execute_tool_run(tool_name, query_text, runner)
    except sqlite3.Error as exc:
        print(f"Could not record {tool_name} run: {exc}. Initialize the database and check its permissions.")
        return 1
    if status == "success":
        print(value)
        print(f"{tool_name.capitalize()} check saved to the database.")
        return 0
    elif status == "timed_out":
        print(f"{tool_name.capitalize()} timed out: {value}")
    else:
        print(f"{tool_name.capitalize()} could not run: {value}")
    return 1


def run_holehe_check(email):
    return _run_check("holehe", email, validate_email, lambda value: run_holehe(value)["output"])


def run_maigret_check(username):
    return _run_check("maigret", username, validate_username, run_maigret)


def run_helix_check(username):
    return _run_check("helix", username, validate_helix_username, run_helix)


def run_sherlock_check(username):
    return _run_check("sherlock", username, validate_sherlock_username, run_sherlock)


def query(query_text):
    try:
        result = query_database(query_text)
    except sqlite3.Error as exc:
        print(
            f"Database query failed: {exc}. Initialize the database and check its permissions."
        )
        return 1
    print(f"Query result: {result}")
    return 0


def query_menu():
    choice = _choose_option(
        [
            "Recent Tool Runs",
            "Saved Tool Results",
            "Enter a Custom SQL Query",
            "Back",
        ],
        "Choose database view",
    )
    if choice == 0:
        query(
            "SELECT runID, toolName, queryText, status, startedAt, finishedAt, "
            "errorText FROM ToolRuns ORDER BY runID DESC LIMIT 20"
        )
    elif choice == 1:
        query(
            "SELECT resultID, toolName, queryText, resultText, createdAt "
            "FROM ToolResults ORDER BY resultID DESC LIMIT 20"
        )
    elif choice == 2:
        query(input("Enter your SQL query: ").strip())


def _cli_initialize_database(_args):
    init_db()
    return 0


def _cli_list_tools(_args):
    list_supported_tools()
    return 0


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="osintbeast",
        description="Run authorized OSINT workflows or start the interactive menu.",
    )
    commands = parser.add_subparsers(dest="command")

    initialize = commands.add_parser("init-db", help="initialize the local database")
    initialize.set_defaults(handler=_cli_initialize_database)

    listing = commands.add_parser("list-tools", help="list registered tools and installation status")
    listing.set_defaults(handler=_cli_list_tools)

    download_command = commands.add_parser("download", help="download a registered tool")
    download_command.add_argument("tool", help="registered tool name")
    download_command.set_defaults(handler=lambda args: download(args.tool))

    install_command = commands.add_parser("install", help="install a supported Python tool")
    install_command.add_argument("tool", help="registered Python tool name")
    install_command.add_argument(
        "--path",
        help="tool project directory (defaults to tools/<tool>)",
    )
    install_command.set_defaults(handler=lambda args: install(args.tool, args.path))

    email_command = commands.add_parser("check-email", help="check an email address with Holehe")
    email_command.add_argument(
        "email",
        help="email address (for example, person@example.com)",
    )
    email_command.set_defaults(handler=lambda args: run_holehe_check(args.email))

    username_command = commands.add_parser(
        "check-username",
        help="check a username with Maigret, Helix, or Sherlock",
    )
    username_command.add_argument(
        "tool",
        choices=("maigret", "helix", "sherlock"),
        help="username search integration",
    )
    username_command.add_argument(
        "username",
        help=(
            "up to 100 ASCII letters, digits, dots, underscores or hyphens; "
            "Helix accepts up to 50 and an optional leading @"
        ),
    )
    username_command.set_defaults(
        handler=lambda args: {
            "maigret": run_maigret_check,
            "helix": run_helix_check,
            "sherlock": run_sherlock_check,
        }[args.tool](args.username)
    )

    query_command = commands.add_parser("query", help="run a SQL query against the local database")
    query_command.add_argument("sql", help="SQL statement to run")
    query_command.set_defaults(handler=lambda args: query(args.sql))
    return parser


def cli(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        while True:
            main_menu()
    try:
        return args.handler(args)
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        print(f"Command failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(cli())
