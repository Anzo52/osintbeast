import getpass
from unittest.mock import Mock

import pytest

import checkers


@pytest.mark.parametrize(
    ("menu", "prompt"),
    [
        (checkers._run_email_check_menu, "Enter the email to check: "),
        (checkers._run_username_check_menu, "Enter the username to check: "),
    ],
)
def test_checker_reads_hidden_input(monkeypatch, menu, prompt):
    reader = Mock(return_value=" value ")
    check = Mock()
    monkeypatch.setattr(checkers.getpass, "getpass", reader)

    menu(check)

    reader.assert_called_once_with(prompt)
    check.assert_called_once_with("value")


@pytest.mark.parametrize(
    "menu", [checkers._run_email_check_menu, checkers._run_username_check_menu]
)
def test_checker_refuses_getpass_fallback_before_reading(monkeypatch, capsys, menu):
    check = Mock()
    stdin = Mock()
    monkeypatch.setattr("sys.stdin", stdin)
    monkeypatch.setattr(checkers.getpass, "getpass", getpass.fallback_getpass)

    menu(check)

    stdin.readline.assert_not_called()
    check.assert_not_called()
    assert capsys.readouterr().out == (
        "Cannot read input securely. Please use an interactive terminal.\n"
    )


@pytest.mark.parametrize("error", [EOFError(), OSError()])
@pytest.mark.parametrize(
    "menu", [checkers._run_email_check_menu, checkers._run_username_check_menu]
)
def test_checker_handles_unavailable_input(monkeypatch, capsys, menu, error):
    check = Mock()
    monkeypatch.setattr(checkers.getpass, "getpass", Mock(side_effect=error))

    menu(check)

    check.assert_not_called()
    assert "Cannot read input securely" in capsys.readouterr().out
