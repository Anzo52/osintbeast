def _read_input(prompt):
    try:
        return input(prompt).strip()
    except (EOFError, OSError):
        print("Cannot read input. Please use an interactive terminal.")
        return None


def _run_email_check_menu(check):
    email = _read_input("Enter the email to check: ")
    if email is not None:
        check(email)


def _run_username_check_menu(check):
    username = _read_input("Enter the username to check: ")
    if username is not None:
        check(username)
