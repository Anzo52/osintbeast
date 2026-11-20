import asyncio
import builtins
import json
from unittest.mock import Mock

import pytest

import maigret_integration


@pytest.mark.parametrize(
    ("username", "expected"),
    [(" user.name-1_ ", "user.name-1_"), ("a" * 100, "a" * 100)],
)
def test_valid_username_normalizes_and_accepts_maximum_length(username, expected):
    assert maigret_integration.validate_username(username) == expected


@pytest.mark.parametrize("username", ["", "  ", "user name", "a" * 101, "../name", None])
def test_invalid_username(username):
    with pytest.raises(ValueError):
        maigret_integration.validate_username(username)


def test_search_uses_installed_maigret_and_all_enabled_sites(monkeypatch):
    maigret = pytest.importorskip("maigret")
    seen = {}

    async def fake_search(**kwargs):
        seen.update(kwargs)
        status = Mock(status="Claimed", error=None)
        failed = Mock(status="Unknown", error="request failed")
        return {
            "Example": {"status": status, "url_user": "https://example.test/user"},
            "Unreachable": {"status": failed},
        }

    monkeypatch.setattr(maigret, "search", fake_search)

    output = json.loads(maigret_integration.run_maigret(" user "))
    assert output == [
        {"site": "Example", "url": "https://example.test/user", "status": "Claimed", "error": None},
        {"site": "Unreachable", "url": "", "status": "Unknown", "error": "request failed"},
    ]
    assert seen["username"] == "user"
    assert len(seen["site_dict"]) > 500
    assert all(not site.disabled for site in seen["site_dict"].values())
    assert seen["no_progressbar"] is True


def test_real_maigret_api_with_mocked_site_check(monkeypatch):
    pytest.importorskip("maigret")
    from maigret import checking
    from maigret.result import MaigretCheckResult, MaigretCheckStatus
    from maigret.sites import MaigretDatabase

    original_rank = MaigretDatabase.ranked_sites_dict

    def one_site(database, **kwargs):
        sites = original_rank(database, **kwargs)
        name = next(iter(sites))
        return {name: sites[name]}

    async def fake_check(site, username, options, logger, notify, **kwargs):
        url = "https://example.test/user"
        status = MaigretCheckResult(username, site.name, url, MaigretCheckStatus.CLAIMED)
        return site.name, {"url_user": url, "status": status}

    monkeypatch.setattr(MaigretDatabase, "ranked_sites_dict", one_site)
    monkeypatch.setattr(checking, "check_site_for_username", fake_check)

    output = json.loads(maigret_integration.run_maigret("user"))
    assert len(output) == 1
    assert output[0]["status"] == "Claimed"
    assert output[0]["url"] == "https://example.test/user"


def test_search_timeout_is_propagated(monkeypatch):
    maigret = pytest.importorskip("maigret")

    async def timeout(**kwargs):
        raise asyncio.TimeoutError("test timeout")

    monkeypatch.setattr(maigret, "search", timeout)
    with pytest.raises(TimeoutError, match="Maigret search timed out"):
        maigret_integration.run_maigret("user")


def test_empty_search_result_is_not_reported_as_success(monkeypatch):
    async def empty(_):
        return {}

    monkeypatch.setattr(maigret_integration, "_search", empty)
    with pytest.raises(RuntimeError, match="no site results"):
        maigret_integration.run_maigret("user")


def test_total_timeout_cancels_search(monkeypatch):
    maigret = pytest.importorskip("maigret")
    cancelled = []

    async def slow(**kwargs):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.append(True)

    monkeypatch.setattr(maigret, "search", slow)
    monkeypatch.setattr(maigret_integration, "MAIGRET_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(TimeoutError):
        maigret_integration.run_maigret("user")
    assert cancelled == [True]


def test_missing_maigret_dependency_is_actionable(monkeypatch):
    original_import = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "maigret":
            raise ModuleNotFoundError("No module named 'maigret'")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    with pytest.raises(RuntimeError, match=r"pip install.*maigret"):
        maigret_integration.run_maigret("user")
