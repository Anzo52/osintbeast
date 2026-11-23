import asyncio
import json
import sys
import types

import pytest

import helix_integration


@pytest.mark.parametrize(
    ("username", "expected"),
    [
        ("user", "user"),
        (" @user.name-1_ ", "user.name-1_"),
        ("a" * 50, "a" * 50),
    ],
)
def test_validate_username_normalizes_accepted_input(username, expected):
    assert helix_integration.validate_username(username) == expected


@pytest.mark.parametrize(
    "username",
    ["", "  ", "@", "@@user", " @@@user ", "user name", "a" * 51, "../name", None],
)
def test_validate_username_rejects_invalid_input(username):
    with pytest.raises(ValueError):
        helix_integration.validate_username(username)


def _install_fake_helix(monkeypatch, scan, left_out_error=None):
    package = types.ModuleType("helix_osint")
    package.__path__ = []
    api = types.ModuleType("helix_osint.api")
    api.scan = scan
    api.LeftOut = left_out_error or type("LeftOut", (Exception,), {})
    monkeypatch.setitem(sys.modules, "helix_osint", package)
    monkeypatch.setitem(sys.modules, "helix_osint.api", api)
    return api.LeftOut


def test_run_helix_calls_builtin_api_and_returns_json(monkeypatch):
    seen = []
    report = {
        "schema": helix_integration.HELIX_REPORT_SCHEMA,
        "username": "user",
        "accounts": [],
        "results": [],
    }

    async def fake_scan(username, **kwargs):
        seen.append((username, kwargs))
        return report

    _install_fake_helix(monkeypatch, fake_scan)

    output = helix_integration.run_helix(" @user ")

    assert seen == [("user", {"sources": "builtin"})]
    expected_report = {**report, "username": "[redacted]"}
    assert json.loads(output) == expected_report


def test_run_helix_times_out_and_cancels_scan(monkeypatch):
    cancelled = []

    async def slow_scan(username, **kwargs):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.append(True)

    _install_fake_helix(monkeypatch, slow_scan)
    monkeypatch.setattr(helix_integration, "HELIX_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(TimeoutError, match="Helix scan timed out"):
        helix_integration.run_helix("user")
    assert cancelled == [True]


def test_run_helix_reports_left_out_username(monkeypatch):
    left_out_error = _install_fake_helix(
        monkeypatch,
        lambda username, **kwargs: None,
    )

    async def refuse(username, **kwargs):
        raise left_out_error("username is on Helix's left-out list")

    monkeypatch.setitem(sys.modules["helix_osint.api"].__dict__, "scan", refuse)

    with pytest.raises(RuntimeError, match="left-out list"):
        helix_integration.run_helix("user")


def test_run_helix_rejects_unexpected_report(monkeypatch):
    async def fake_scan(username, **kwargs):
        return {"schema": "unknown"}

    _install_fake_helix(monkeypatch, fake_scan)

    with pytest.raises(RuntimeError, match="unsupported report"):
        helix_integration.run_helix("user")


def test_run_helix_missing_dependency_is_actionable(monkeypatch):
    monkeypatch.setitem(sys.modules, "helix_osint", None)
    monkeypatch.setitem(sys.modules, "helix_osint.api", None)

    with pytest.raises(RuntimeError, match=r"pip install.*\[helix\]"):
        helix_integration.run_helix("user")
