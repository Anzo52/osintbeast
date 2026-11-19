import subprocess
import zipfile
import zlib
from unittest.mock import Mock

import pytest

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


def test_query_reports_sqlite_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "test.db"))
    database.setup_database()

    main.query("SELECT * FROM missing_table")

    assert capsys.readouterr().out == (
        "Database query failed: no such table: missing_table\n"
    )


@pytest.mark.parametrize(
    ("choices", "inputs", "failure"),
    [
        ([2, 2, 1], [], "Download failed: Unable to download repository archive"),
        ([3, 1, 0, 1], [], "Installation failed: Tool 'holehe' is not supported."),
        ([5, 2, 1], ["SELECT * FROM missing_table"], "Database query failed: no such table: missing_table"),
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
    monkeypatch.setattr(main, "query_database", lambda query: [(1,)])
    monkeypatch.setattr("builtins.input", lambda prompt: "")

    main.download("holehe")
    main.install("holehe")
    main.query("SELECT 1")

    assert capsys.readouterr().out == (
        "holehe downloaded successfully to tools/holehe.\n"
        "holehe installed successfully.\n"
        "Query result: [(1,)]\n"
    )


def test_helix_menu_action_dispatches_username(monkeypatch):
    class FakeMenu:
        def __init__(self, options, title):
            assert "Run Helix Check" in options

        def show(self):
            return 7

    runner = Mock()
    monkeypatch.setattr(main, "TerminalMenu", FakeMenu)
    monkeypatch.setattr("builtins.input", lambda prompt: " @user ")
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
    monkeypatch.setattr("builtins.input", lambda prompt: " person@example.com ")
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
    monkeypatch.setattr("builtins.input", lambda prompt: " user ")
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


def test_query_menu_offers_recent_runs_without_custom_sql(monkeypatch):
    query = Mock()

    class FakeMenu:
        def __init__(self, options, title):
            assert options == [
                "Recent Tool Runs",
                "Saved Tool Results",
                "Enter a Custom SQL Query",
                "Back",
            ]

        def show(self):
            return 0

    monkeypatch.setattr(main, "TerminalMenu", FakeMenu)
    monkeypatch.setattr(menu_utils, "TerminalMenu", FakeMenu)
    monkeypatch.setattr(main, "query", query)

    main.query_menu()

    assert query.call_args.args[0].startswith("SELECT runID, toolName, queryText, status")
