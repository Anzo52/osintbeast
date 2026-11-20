from getpass import getpass


def _run_email_check_menu(check):
    email = getpass("Enter the email to check: ").strip()
    check(email)


def _run_username_check_menu(check):
    username = getpass("Enter the username to check: ").strip()
    check(username)
