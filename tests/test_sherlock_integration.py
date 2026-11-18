import builtins
import json
import sys
import types
from unittest.mock import Mock

import pytest

import sherlock_integration


@pytest.mark.parametrize(
    ("username", "expected"),
    [("user", "user"), (" user.name-1_ ", "user.name-1_")],
)
def test_validate_username_normalizes_accepted_input(username, expected):
    assert sherlock_integration.validate_username(username) == expected


@pytest.mark.parametrize(
    "username",
    ["", "  ", "user name", "a" * 101, "../name", None],
)
def test_validate_username_rejects_invalid_input(username):
    with pytest.raises(ValueError):
        sherlock_integration.validate_username(username)


def test_run_sherlock_uses_bundled_non_nsfw_sites(monkeypatch, tmp_path):
    seen = {}

    class FakeSitesInformation:
        def __init__(self, data_file_path):
            seen["data_file_path"] = data_file_path
            self.sites = {"Example": {}}

        def remove_nsfw_sites(self):
            seen["removed_nsfw"] = True

    def fake_sherlock(username, **kwargs):
        seen["username"] = username
        seen.update(kwargs)
        status = Mock(status="Claimed", context=None)
        return {
            "Example": {
                "status": status,
                "url_user": "https://example.test/user",
                "http_status": 200,
            }
        }

    package = types.ModuleType("sherlock_project")
    package.__path__ = []
    notify = types.ModuleType("sherlock_project.notify")
    notify.QueryNotify = Mock
    sherlock = types.ModuleType("sherlock_project.sherlock")
    sherlock.sherlock = fake_sherlock
    sites = types.ModuleType("sherlock_project.sites")
    sites.SitesInformation = FakeSitesInformation
    monkeypatch.setitem(sys.modules, "sherlock_project", package)
    monkeypatch.setitem(sys.modules, "sherlock_project.notify", notify)
    monkeypatch.setitem(sys.modules, "sherlock_project.sherlock", sherlock)
    monkeypatch.setitem(sys.modules, "sherlock_project.sites", sites)
    monkeypatch.setattr(sherlock_integration, "files", lambda _: tmp_path)

    output = json.loads(sherlock_integration.run_sherlock(" user "))

    assert output == [
        {
            "site": "Example",
            "url": "https://example.test/user",
            "status": "Claimed",
            "http_status": 200,
            "error": None,
        }
    ]
    assert seen["username"] == "user"
    assert seen["data_file_path"].endswith("resources/data.json")
    assert seen["removed_nsfw"] is True
    assert seen["timeout"] == sherlock_integration.SHERLOCK_REQUEST_TIMEOUT_SECONDS
    assert isinstance(seen["query_notify"], Mock)


def test_run_sherlock_rejects_empty_results(monkeypatch):
    monkeypatch.setattr(sherlock_integration, "_search", lambda _: {})

    with pytest.raises(RuntimeError, match="no site results"):
        sherlock_integration.run_sherlock("user")


def test_run_sherlock_rejects_result_without_status(monkeypatch):
    monkeypatch.setattr(sherlock_integration, "_search", lambda _: {"Example": {}})

    with pytest.raises(RuntimeError, match="without status"):
        sherlock_integration.run_sherlock("user")


def test_missing_sherlock_dependency_is_actionable(monkeypatch):
    original_import = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "sherlock_project.notify":
            raise ModuleNotFoundError("No module named 'sherlock_project'")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)

    with pytest.raises(RuntimeError, match=r"pip install.*\[sherlock\]"):
        sherlock_integration.run_sherlock("user")
