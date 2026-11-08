import pytest

import holehe_integration


@pytest.mark.parametrize(
    "email",
    [
        "user@example.com",
        " first.last+tag@sub.example.co.uk ",
        "user@exämple.com",
    ],
)
def test_validate_email_accepts_well_formed_addresses(email):
    assert holehe_integration.validate_email(email) == email.strip()


@pytest.mark.parametrize(
    "email",
    [
        "",
        "not-an-email",
        "@example.com",
        "user@",
        "user@@example.com",
        "user name@example.com",
        "user..name@example.com",
        ".user@example.com",
        "user@example",
        "user@example..com",
        "user@-example.com",
        "user@example-.com",
        "user@example.com.",
        "a" * 65 + "@example.com",
        "a" * 250 + "@example.com",
        None,
    ],
)
def test_validate_email_rejects_malformed_addresses(email):
    with pytest.raises(ValueError, match="valid email address"):
        holehe_integration.validate_email(email)


def test_invalid_email_does_not_launch_holehe(monkeypatch):
    def fail_if_called(*args, **kwargs):
        pytest.fail("Holehe must not launch for invalid input")

    monkeypatch.setattr(holehe_integration.subprocess, "run", fail_if_called)

    with pytest.raises(ValueError):
        holehe_integration.run_holehe("broken@@example.com")
