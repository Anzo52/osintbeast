def _read_input(prompt):
    return input(prompt).strip()


def _run_email_check_menu(check):
    check(_read_input("Enter the email to check: "))


def _run_username_check_menu(check):
    check(_read_input("Enter the username to check: "))
