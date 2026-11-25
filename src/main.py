import argparse
import os
import subprocess
import sys
import tempfile
import zipfile
import zlib
from pathlib import Path

from simple_term_menu import TerminalMenu

from backend_services import get_recent_tool_runs, get_saved_tool_results
from checkers import _run_email_check_menu, _run_username_check_menu
from data.utils import database
from data.utils.database import (
    export_tool_runs_json,
    get_tool_run,
    list_tool_runs,
)
from data.utils.init_db import init_db
from downloader import download_tool
from helix_integration import run_helix
from helix_integration import validate_username as validate_helix_username
from holehe_integration import run_holehe, validate_email
from installer import install_tool
from maigret_integration import run_maigret, validate_username
from menu_utils import _choose_option, _download_tool_menu, _install_tool_menu
from sherlock_integration import run_sherlock
from sherlock_integration import validate_username as validate_sherlock_username
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
        0: initialize_database,
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


def initialize_database():
    try:
        init_db()
    except (OSError, UnicodeError, *database.DATABASE_ERRORS) as exc:
        print(f"Database initialization failed: {exc}")


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
        status, value = execute_tool_run(
            tool_name, query_text, runner, sensitive_input=True
        )
    except (OSError, *database.DATABASE_ERRORS) as exc:
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


def query_view(view):
    try:
        database.setup_database()
        if view == "runs":
            result = get_recent_tool_runs()
        else:
            result = get_saved_tool_results()
    except (OSError, UnicodeError, *database.DATABASE_ERRORS) as exc:
        print(
            f"Database query failed: {exc}. Initialize the database and check its permissions."
        )
        return 1
    print(f"Query result: {result}")
    return 0


def query_menu():
    choice = _choose_option(
        [
            "Run History",
            "Saved Tool Results",
            "Back",
        ],
        "Choose database view",
    )
    if choice in (0, 1):
        try:
            database.setup_database()
        except (OSError, UnicodeError, *database.DATABASE_ERRORS) as exc:
            print(f"Database initialization failed: {exc}")
            return
    if choice == 0:
        run_history_menu()
    elif choice == 1:
        return query_view("results")


def show_run_history(tool_name=None, status=None, offset=0, cursor=None):
    try:
        runs = list_tool_runs(
            limit=20, offset=offset, tool_name=tool_name, status=status,
            cursor=cursor, redact_sensitive=True,
        )
    except (OSError, *database.DATABASE_ERRORS) as exc:
        print(f"Run history query failed: {exc}")
        return []
    except ValueError as exc:
        print(f"Invalid history filter: {exc}")
        return []
    if not runs:
        print("No matching runs.")
        return []
    for run_id, tool, query_text, run_status, started, finished, _error in runs:
        summary = query_text[:80] + ("…" if len(query_text) > 80 else "")
        print(
            f"Run {run_id} | {tool} | input={summary!r} | {run_status} | "
            f"started={started} | finished={finished or '—'}"
        )
    return runs


def inspect_tool_run(run_id):
    try:
        run = get_tool_run(run_id, redact_sensitive=True)
    except ValueError as exc:
        print(f"Could not inspect run: {exc}")
        return
    except (OSError, *database.DATABASE_ERRORS) as exc:
        print(f"Could not inspect run: {exc}")
        return
    if run is None:
        print(f"Run {run_id} was not found.")
        return
    print(
        f"Run {run[0]} | {run[1]} | input={run[2]!r} | {run[3]} | "
        f"started={run[6]} | finished={run[7] or '—'}"
    )
    print(f"Result: {run[4]!r}" if run[4] else "Result: (empty)")
    if run[5] is not None:
        print(f"Error: {run[5]!r}")


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


def _write_export_file(output_path, content):
    destination = Path(output_path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    prefix = f".{destination.name}."
    descriptor, temporary_path = tempfile.mkstemp(
        prefix=prefix, suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as export_file:
            export_file.write(content + "\n")
            export_file.flush()
            os.fsync(export_file.fileno())
        os.link(temporary_path, destination)
    finally:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass


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
    if include_sensitive and os.name == "nt":
        print(
            "Sensitive exports are disabled on Windows because file permissions "
            "are inherited from the destination directory."
        )
        return
    try:
        content = export_tool_runs_json(
            run_ids, include_sensitive=include_sensitive
        )
        _write_export_file(output_path, content)
    except (OSError, RuntimeError, ValueError, *database.DATABASE_ERRORS) as exc:
        print(f"Could not export runs: {exc}")
        return
    print(f"Exported {len(run_ids)} run(s) to {output_path}.")


def run_history_menu():
    tool_name = None
    status = None
    page_cursors = [None]
    current_runs = show_run_history(tool_name, status)
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
            current_runs = show_run_history(
                tool_name, status, cursor=page_cursors[-1]
            )
        elif choice == 1:
            tool_value = input("Tool name (blank for any): ").strip()
            status_value = input(
                "Status (running, success, failed, timed_out; blank for any): "
            ).strip()
            tool_name = tool_value or None
            status = status_value or None
            page_cursors = [None]
            try:
                list_tool_runs(
                    limit=1, tool_name=tool_name, status=status
                )
            except ValueError as exc:
                print(f"Invalid history filter: {exc}")
                tool_name = status = None
            except (OSError, *database.DATABASE_ERRORS) as exc:
                print(f"Run history query failed: {exc}")
                continue
            current_runs = show_run_history(tool_name, status)
        elif choice == 2:
            value = input("Run ID to inspect: ").strip()
            try:
                inspect_tool_run(int(value))
            except ValueError:
                print("Enter a valid run ID.")
        elif choice == 3:
            if len(current_runs) == 20:
                page_cursors.append(current_runs[-1][0])
                current_runs = show_run_history(
                    tool_name, status, cursor=page_cursors[-1]
                )
        elif choice == 4:
            if len(page_cursors) > 1:
                page_cursors.pop()
                current_runs = show_run_history(
                    tool_name, status, cursor=page_cursors[-1]
                )
        elif choice == 5:
            export_selected_runs()


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
        help=(
            "email address (for example, person@example.com); "
            "use -- before an address beginning with -"
        ),
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
            "Helix accepts up to 50 and an optional leading @; "
            "use -- before a username beginning with -"
        ),
    )
    username_command.set_defaults(
        handler=lambda args: {
            "maigret": run_maigret_check,
            "helix": run_helix_check,
            "sherlock": run_sherlock_check,
        }[args.tool](args.username)
    )

    query_command = commands.add_parser(
        "query", help="view recent tool runs or saved tool results"
    )
    query_command.add_argument(
        "view",
        choices=("runs", "results"),
        help="which saved data to view",
    )
    query_command.set_defaults(handler=lambda args: query_view(args.view))
    return parser


def cli(argv=None):
    parser = _build_parser()
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] == "check-email" and len(argv) > 1:
        if argv[1].startswith("-") and argv[1] not in ("--", "-h", "--help"):
            argv.insert(1, "--")
    elif argv and argv[0] == "check-username" and len(argv) > 2:
        if argv[2].startswith("-") and argv[2] not in ("--", "-h", "--help"):
            argv.insert(2, "--")
    args = parser.parse_args(argv)
    if args.command is None:
        while True:
            main_menu()
    try:
        return args.handler(args)
    except (OSError, RuntimeError, ValueError, *database.DATABASE_ERRORS) as exc:
        print(f"Command failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(cli())
