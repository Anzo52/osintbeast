import sqlite3
import subprocess
import zipfile
import zlib
from unittest.mock import Mock

import pytest

import checkers
import data.utils.database as database
import main
import menu_utils


def test_list_supported_tools_shows_current_installation_status(monkeypatch, capsys):
    monkeypatch.setattr(main, "get_supported_tools", lambda: ["amass", "holehe"])
    installed = {"amass"}
    monkeypatch.setattr(main, "is_tool_installed", lambda name: name in installed)

    main.list_supported_tools()
    assert capsys.readouterr().out == "- amass: installed\n- holehe: not installed\n"

    installed.add("holehe")
    main.list_supported_tools()
    assert capsys.readouterr().out == "- amass: installed\n- holehe: installed\n"


@pytest.mark.parametrize("choice", [None, "0", -1, 10])
def test_main_menu_ignores_cancelled_or_invalid_choices(monkeypatch, choice):
    class FakeMenu:
        def __init__(self, options, title):
            assert title == "OSINT Beast Main Menu"

        def show(self):
            return choice

    action = Mock()
    monkeypatch.setattr(main, "TerminalMenu", FakeMenu)
    monkeypatch.setattr(main, "init_db", action)

    main.main_menu()

    action.assert_not_called()


@pytest.mark.parametrize(
    ("choice", "runner_name", "prompt"),
    [
        (4, "run_holehe_check", "Enter the email to check: "),
        (6, "run_maigret_check", "Enter the username to check: "),
        (7, "run_helix_check", "Enter the username to check: "),
        (8, "run_sherlock_check", "Enter the username to check: "),
    ],
)
def test_visible_target_flag_reaches_each_menu_check(monkeypatch, choice, runner_name, prompt):
    choices = iter([choice, 9])

    class FakeMenu:
        def __init__(self, options, title):
            pass

        def show(self):
            return next(choices)

    runner = Mock()
    reader = Mock(return_value=" target ")
    monkeypatch.setattr(main, "TerminalMenu", FakeMenu)
    monkeypatch.setattr(main, runner_name, runner)
    monkeypatch.setattr("builtins.input", reader)
    monkeypatch.setattr(checkers, "getpass", Mock(side_effect=AssertionError("unexpected getpass")))

    with pytest.raises(SystemExit) as exc:
        main.cli(["--show-target-input"])

    assert exc.value.code == 0
    reader.assert_called_once_with(prompt)
    runner.assert_called_once_with("target")


def test_visible_target_flag_rejects_direct_commands(capsys):
    with pytest.raises(SystemExit) as exc:
        main.cli(["--show-target-input", "list-tools"])

    assert exc.value.code == 2
    assert "only available for the interactive menu" in capsys.readouterr().err


def test_menu_reports_initialization_failure(monkeypatch, capsys):
    def fail():
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(main, "init_db", fail)

    main.menu_actions()[0]()

    assert capsys.readouterr().out == (
        "Database initialization failed: unable to open database file\n"
    )


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (ValueError("Unsupported tool: unknown"), "Unsupported tool: unknown"),
        (RuntimeError("Unable to download repository archive"), "Unable to download repository archive"),
        (PermissionError("permission denied"), "permission denied"),
        (zipfile.BadZipFile("File is not a zip file"), "File is not a zip file"),
        (zlib.error("invalid compressed data"), "invalid compressed data"),
        (EOFError("truncated archive"), "truncated archive"),
        (NotImplementedError("unsupported compression"), "unsupported compression"),
    ],
)
def test_download_reports_errors(monkeypatch, capsys, error, message):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(main, "download_tool", fail)

    main.download("unknown")

    assert capsys.readouterr().out == f"Download failed: {message}\n"


@pytest.mark.parametrize(
    "error",
    [
        ValueError("Tool is not supported"),
        FileNotFoundError("Tool directory does not exist"),
        PermissionError("permission denied"),
        subprocess.CalledProcessError(1, ["pip"]),
        subprocess.TimeoutExpired(["pip"], 600),
    ],
)
def test_install_reports_errors(monkeypatch, capsys, error):
    monkeypatch.setattr("builtins.input", lambda prompt: "")

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(main, "install_tool", fail)

    main.install("holehe")

    assert capsys.readouterr().out == f"Installation failed: {error}\n"


def test_query_reports_sqlite_error(monkeypatch, capsys):
    monkeypatch.setattr(
        main,
        "get_recent_tool_runs",
        Mock(side_effect=sqlite3.OperationalError("database is locked")),
    )

    assert main.query_view("runs") == 1
    assert capsys.readouterr().out == (
        "Database query failed: database is locked. "
        "Initialize the database and check its permissions.\n"
    )


@pytest.mark.parametrize(
    ("choices", "inputs", "failure"),
    [
        ([2, 2, 1], [], "Download failed: Unable to download repository archive"),
        ([3, 1, 0, 1], [], "Installation failed: Tool 'holehe' is not supported."),
    ],
)
def test_menu_remains_usable_after_failure(
    tmp_path, monkeypatch, capsys, choices, inputs, failure
):
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "test.db"))
    database.setup_database()
    choices = iter(choices)
    answers = iter(inputs)

    class FakeMenu:
        def __init__(self, options, title):
            pass

        def show(self):
            return next(choices)

    monkeypatch.setattr(main, "TerminalMenu", FakeMenu)
    monkeypatch.setattr(menu_utils, "TerminalMenu", FakeMenu)
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr(
        main,
        "download_tool",
        Mock(side_effect=RuntimeError("Unable to download repository archive")),
    )
    monkeypatch.setattr(
        main,
        "install_tool",
        Mock(side_effect=ValueError("Tool 'holehe' is not supported.")),
    )

    main.main_menu()
    main.main_menu()

    output = capsys.readouterr().out
    assert failure in output
    assert "- holehe" in output


def test_successful_actions_keep_their_output(monkeypatch, capsys):
    monkeypatch.setattr(main, "download_tool", lambda *args, **kwargs: "tools/holehe")
    monkeypatch.setattr(main, "install_tool", lambda *args, **kwargs: None)

    main.download("holehe")
    main.install("holehe")

    assert capsys.readouterr().out == (
        "holehe downloaded successfully to tools/holehe.\n"
        "holehe installed successfully.\n"
    )


def test_helix_menu_action_dispatches_username(monkeypatch):
    class FakeMenu:
        def __init__(self, options, title):
            assert "Run Helix Check" in options

        def show(self):
            return 7

    runner = Mock()
    monkeypatch.setattr(main, "TerminalMenu", FakeMenu)
    monkeypatch.setattr(checkers, "getpass", lambda prompt: " @user ")
    monkeypatch.setattr(main, "run_helix_check", runner)

    main.main_menu()

    runner.assert_called_once_with("@user")


def test_holehe_menu_action_dispatches_email(monkeypatch):
    class FakeMenu:
        def __init__(self, options, title):
            self.options = options

        def show(self):
            return self.options.index("Run Holehe Check")

    runner = Mock()
    monkeypatch.setattr(main, "TerminalMenu", FakeMenu)
    monkeypatch.setattr(checkers, "getpass", lambda prompt: " person@example.com ")
    monkeypatch.setattr(main, "run_holehe_check", runner)

    main.main_menu()

    runner.assert_called_once_with("person@example.com")


def test_sherlock_menu_action_dispatches_username(monkeypatch):
    class FakeMenu:
        def __init__(self, options, title):
            assert "Run Sherlock Check" in options

        def show(self):
            return 8

    runner = Mock()
    monkeypatch.setattr(main, "TerminalMenu", FakeMenu)
    monkeypatch.setattr(checkers, "getpass", lambda prompt: " user ")
    monkeypatch.setattr(main, "run_sherlock_check", runner)

    main.main_menu()

    runner.assert_called_once_with("user")


def test_download_menu_selects_registered_tool_without_typing(monkeypatch):
    choices = iter([2, 1])

    class FakeMenu:
        def __init__(self, options, title):
            if title == "OSINT Beast Main Menu":
                assert "Download Tool" in options
            else:
                assert title == "Select a tool to download"
                assert "helix" in options

        def show(self):
            return next(choices)

    download = Mock()
    monkeypatch.setattr(main, "TerminalMenu", FakeMenu)
    monkeypatch.setattr(menu_utils, "TerminalMenu", FakeMenu)
    monkeypatch.setattr(main, "download", download)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("unexpected text prompt"))

    main.main_menu()

    download.assert_called_once_with("helix")


def test_install_defaults_to_download_folder_without_prompt(monkeypatch):
    install_tool = Mock()
    monkeypatch.setattr(main, "install_tool", install_tool)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("unexpected text prompt"))

    main.install("Helix")

    install_tool.assert_called_once_with("Helix", "tools/helix")


def test_install_menu_selects_pip_tool_and_default_source(monkeypatch):
    choices = iter([3, 0, 0])

    class FakeMenu:
        def __init__(self, options, title):
            if title == "Select a Python tool to install":
                assert "amass" not in options
                assert "helix" in options
            elif title == "Install helix":
                assert options == [
                    "Use downloaded copy",
                    "Choose another folder",
                    "Back",
                ]

        def show(self):
            return next(choices)

    install = Mock()
    monkeypatch.setattr(main, "TerminalMenu", FakeMenu)
    monkeypatch.setattr(menu_utils, "TerminalMenu", FakeMenu)
    monkeypatch.setattr(main, "install", install)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("unexpected text prompt"))

    main.main_menu()

    install.assert_called_once_with("helix")


def test_query_menu_offers_structured_run_history(monkeypatch):
    history = Mock()

    class FakeMenu:
        def __init__(self, options, title):
            assert options == [
                "Run History",
                "Saved Tool Results",
                "Back",
            ]

        def show(self):
            return 0

    monkeypatch.setattr(main, "TerminalMenu", FakeMenu)
    monkeypatch.setattr(menu_utils, "TerminalMenu", FakeMenu)
    monkeypatch.setattr(database, "setup_database", Mock())
    monkeypatch.setattr(main, "run_history_menu", history)

    main.query_menu()

    history.assert_called_once_with()


@pytest.mark.parametrize("choice", [0, 1])
def test_saved_views_redact_legacy_records(tmp_path, monkeypatch, capsys, choice):
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "legacy.db"))
    database.setup_database()
    with database.connect_db() as conn:
        conn.execute(
            "INSERT INTO ToolResults (toolName, queryText, resultText) "
            "VALUES ('holehe', 'private@example.com', 'private@example.com result')"
        )
        conn.execute(
            "INSERT INTO ToolRuns (toolName, queryText, status, errorText) "
            "VALUES ('holehe', 'private@example.com', 'failed', 'private@example.com error')"
        )
        run_id = conn.execute(
            "SELECT runID FROM ToolRuns ORDER BY runID DESC LIMIT 1"
        ).fetchone()[0]
    choices = iter((choice, 2, 6) if choice == 0 else (choice, 6))
    monkeypatch.setattr(main, "_choose_option", lambda *args: next(choices))
    monkeypatch.setattr("builtins.input", lambda *_args: str(run_id))

    main.query_menu()

    output = capsys.readouterr().out
    assert "private@example.com" not in output
    assert "holehe" in output
    if choice == 0:
        assert f"Run {run_id} | holehe" in output
        assert "input='[redacted]'" in output
        assert "Error: '[redacted] error'" in output
    else:
        assert "SavedToolResult" in output
        assert "result_text='[redacted] result'" in output


def test_saved_view_does_not_query_if_initialization_fails(monkeypatch, capsys):
    monkeypatch.setattr(main, "_choose_option", lambda *args: 0)
    monkeypatch.setattr(
        database, "setup_database", Mock(side_effect=sqlite3.OperationalError("failed"))
    )
    history = Mock()
    monkeypatch.setattr(main, "run_history_menu", history)

    main.query_menu()

    history.assert_not_called()
    assert "Database initialization failed" in capsys.readouterr().out


def test_cli_help_lists_commands_and_input_formats(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main.cli(["--help"])

    assert exc_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "check-email" in help_text
    assert "check-username" in help_text
    assert "init-db" in help_text

    with pytest.raises(SystemExit) as exc_info:
        main.cli(["check-email", "--help"])

    assert exc_info.value.code == 0
    assert "check-email [-h] email" in capsys.readouterr().out

    with pytest.raises(SystemExit) as exc_info:
        main.cli(["check-username", "--help"])

    assert exc_info.value.code == 0
    username_help = capsys.readouterr().out
    assert "ASCII letters" in username_help
    assert "underscores" in username_help
    assert "hyphens" in username_help


def test_cli_requires_command_arguments(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main.cli(["check-email"])

    assert exc_info.value.code == 2
    assert "the following arguments are required: email" in capsys.readouterr().err


def test_cli_rejects_invalid_email_without_running_tool(monkeypatch, capsys):
    runner = Mock()
    monkeypatch.setattr(main, "run_holehe", runner)

    result = main.cli(["check-email", "not-an-email"])

    assert result == 2
    assert "Input error: A valid email address is required." in capsys.readouterr().out
    runner.assert_not_called()


@pytest.mark.parametrize(
    ("arguments", "runner_name", "identifier", "runner_output"),
    [
        (
            ["check-email", "--", "-person@example.com"],
            "run_holehe",
            "-person@example.com",
            {"output": "output"},
        ),
        (
            ["check-username", "sherlock", "--", "-user"],
            "run_sherlock",
            "-user",
            "output",
        ),
    ],
)
def test_cli_accepts_hyphen_identifiers_after_separator(
    tmp_path, monkeypatch, arguments, runner_name, identifier, runner_output
):
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "cli.db"))
    database.setup_database()
    runner = Mock(return_value=runner_output)
    monkeypatch.setattr(main, runner_name, runner)

    assert main.cli(arguments) == 0

    runner.assert_called_once_with(identifier)


def test_cli_rejects_invalid_username_with_accepted_format(monkeypatch, capsys):
    runner = Mock()
    monkeypatch.setattr(main, "run_sherlock", runner)

    result = main.cli(["check-username", "sherlock", "invalid username"])

    assert result == 2
    assert (
        "Use a username of up to 100 letters, digits, dots, underscores or hyphens."
        in capsys.readouterr().out
    )
    runner.assert_not_called()


@pytest.mark.parametrize(
    ("argv", "runner_name", "identifier"),
    [
        (["check-email", "-person@example.com"], "run_holehe_check", "-person@example.com"),
        *[
            (["check-username", tool, "-user"], f"run_{tool}_check", "-user")
            for tool in ("maigret", "helix", "sherlock")
        ],
    ],
)
@pytest.mark.parametrize("help_flag", [None, "-h", "--help"])
@pytest.mark.parametrize("use_sys_argv", [False, True])
def test_cli_hyphen_identifiers_preserve_trailing_help(
    monkeypatch, capsys, argv, runner_name, identifier, help_flag, use_sys_argv
):
    runner = Mock(return_value=0)
    monkeypatch.setattr(main, runner_name, runner)
    arguments = [*argv, *([help_flag] if help_flag else [])]
    monkeypatch.setattr(main.sys, "argv", ["osintbeast", *arguments])
    original = arguments.copy()

    if help_flag:
        with pytest.raises(SystemExit) as exc_info:
            main.cli(None if use_sys_argv else arguments)
        assert exc_info.value.code == 0
        assert f"usage: osintbeast {argv[0]}" in capsys.readouterr().out
        runner.assert_not_called()
    else:
        assert main.cli(None if use_sys_argv else arguments) == 0
        runner.assert_called_once_with(identifier)
    assert arguments == original


@pytest.mark.parametrize(
    "argv",
    [
        ["check-email", "--", "--help"],
        ["check-username", "maigret", "--", "--help"],
    ],
)
def test_cli_explicit_end_of_options_preserves_literal_help(monkeypatch, argv):
    runner = Mock(return_value=0)
    runner_name = "run_holehe_check" if argv[0] == "check-email" else "run_maigret_check"
    monkeypatch.setattr(main, runner_name, runner)

    assert main.cli(argv) == 0
    runner.assert_called_once_with("--help")


@pytest.mark.parametrize(
    ("argv", "runner_name", "identifier"),
    [
        (["check-username", "maigret", "-user"], "run_maigret_check", "-user"),
    ],
)
def test_cli_accepts_identifiers_starting_with_hyphen(
    monkeypatch, argv, runner_name, identifier
):
    runner = Mock(return_value=0)
    monkeypatch.setattr(main, runner_name, runner)
    assert main.cli(argv) == 0
    runner.assert_called_once_with(identifier)


def test_cli_reports_optional_dependency_failure_and_nonzero_status(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "test.db"))
    database.setup_database()

    def missing_dependency(username):
        raise RuntimeError(
            "Maigret is required. Install it with `python -m pip install '.[maigret]'`."
        )

    monkeypatch.setattr(main, "run_maigret", missing_dependency)

    result = main.cli(["check-username", "maigret", "valid_user"])

    assert result == 1
    assert (
        "Maigret could not run: Maigret is required. "
        "Install it with `python -m pip install '.[maigret]'`."
    ) in capsys.readouterr().out


def test_cli_returns_nonzero_for_database_execution_error(monkeypatch, capsys):
    monkeypatch.setattr(database, "setup_database", Mock())
    monkeypatch.setattr(
        main,
        "get_recent_tool_runs",
        Mock(side_effect=sqlite3.OperationalError("database is locked")),
    )

    result = main.cli(["query", "runs"])

    assert result == 1
    assert "Initialize the database and check its permissions." in capsys.readouterr().out


@pytest.mark.parametrize("view", ["runs", "results"])
def test_cli_query_redacts_legacy_records(tmp_path, monkeypatch, capsys, view):
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "legacy-cli.db"))
    database.setup_database()
    with database.connect_db() as conn:
        conn.execute(
            "INSERT INTO ToolResults (toolName, queryText, resultText) "
            "VALUES ('holehe', 'private@example.com', 'private@example.com result')"
        )
        conn.execute(
            "INSERT INTO ToolRuns (toolName, queryText, status, errorText) "
            "VALUES ('holehe', 'private@example.com', 'failed', 'private@example.com error')"
        )

    assert main.cli(["query", view]) == 0
    output = capsys.readouterr().out
    assert "private@example.com" not in output
    assert "[redacted]" in output
