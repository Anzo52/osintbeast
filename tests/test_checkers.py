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
def test_checker_reads_visible_input(monkeypatch, menu, prompt):
    reader = Mock(return_value=" value ")
    check = Mock()
    monkeypatch.setattr("builtins.input", reader)

    menu(check)

    reader.assert_called_once_with(prompt)
    check.assert_called_once_with("value")


@pytest.mark.parametrize("error", [EOFError(), OSError()])
@pytest.mark.parametrize(
    "menu", [checkers._run_email_check_menu, checkers._run_username_check_menu]
)
def test_checker_handles_unavailable_input(monkeypatch, capsys, menu, error):
    check = Mock()
    monkeypatch.setattr("builtins.input", Mock(side_effect=error))

    menu(check)

    check.assert_not_called()
    assert "Cannot read input" in capsys.readouterr().out
