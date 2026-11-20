import builtins
import json
import multiprocessing
import sys
import threading
import time
import types
from unittest.mock import Mock

import pytest

import sherlock_integration


def _spawned_search(username):
    if username != "user":
        return {}
    status = types.SimpleNamespace(status="Claimed", context=None)
    return {
        "Example": {
            "status": status,
            "url_user": f"https://example.test/{username}",
            "http_status": 200,
        }
    }


def _slow_search(_):
    time.sleep(10)


def _large_search(_):
    status = types.SimpleNamespace(status="Claimed", context="x" * 2000)
    return {"Example": {"status": status, "url_user": "", "http_status": 200}}


def _run_search_worker(search):
    receiver, sender = multiprocessing.Pipe(duplex=False)
    try:
        sherlock_integration._search_worker("user", sender, search)
        payload = bytearray()
        while receiver.poll():
            try:
                payload.extend(receiver.recv_bytes(
                    sherlock_integration._WORKER_MESSAGE_CHUNK_SIZE
                ))
            except EOFError:
                break
        return json.loads(payload)
    finally:
        receiver.close()


@pytest.mark.parametrize(
    ("username", "expected"),
    [
        ("user", "user"),
        (" user.name-1_ ", "user.name-1_"),
        ("a" * 100, "a" * 100),
    ],
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

    sherlock_integration._search("user")
    output = json.loads(
        sherlock_integration.run_sherlock(
            " user ",
            process_context=multiprocessing.get_context("spawn"),
            search_function=_spawned_search,
        )
    )

    assert output[0]["site"] == "Example"
    assert output[0]["url"] == "https://example.test/[redacted]"
    assert output[0]["status"] == "Claimed"
    assert seen["username"] == "user"
    assert seen["data_file_path"].endswith("resources/data.json")
    assert seen["removed_nsfw"] is True
    assert seen["timeout"] == sherlock_integration.SHERLOCK_REQUEST_TIMEOUT_SECONDS
    assert isinstance(seen["query_notify"], Mock)


def test_search_worker_rejects_empty_results():
    assert _run_search_worker(lambda _: {}) == {
        "outcome": "failed",
        "message": "Sherlock returned no site results.",
    }


def test_search_worker_rejects_result_without_status():
    assert _run_search_worker(lambda _: {"Example": {}}) == {
        "outcome": "failed",
        "message": "Sherlock returned a result without status for Example.",
    }


def test_search_worker_sends_results_across_multiple_pipe_chunks():
    response = _run_search_worker(_large_search)

    assert response["outcome"] == "success"
    assert len(response["message"][0]["error"]) == 2000


def test_run_sherlock_rejects_additional_scan_when_at_capacity(monkeypatch):
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(sherlock_integration, "_WORKER_SLOTS", slots)
    assert slots.acquire(blocking=False)

    with pytest.raises(RuntimeError, match="busy"):
        sherlock_integration.run_sherlock("user")


def test_run_sherlock_timeout_stops_worker_and_allows_next_run(monkeypatch):
    monkeypatch.setattr(sherlock_integration, "SHERLOCK_TIMEOUT_SECONDS", 0.5)
    child_pids_before = {worker.pid for worker in multiprocessing.active_children()}

    with pytest.raises(TimeoutError, match="overall limit"):
        sherlock_integration.run_sherlock(
            "user",
            process_context=multiprocessing.get_context("spawn"),
            search_function=_slow_search,
        )

    child_pids_after = {worker.pid for worker in multiprocessing.active_children()}
    assert child_pids_after <= child_pids_before
    assert json.loads(
        sherlock_integration.run_sherlock(
            "user",
            process_context=multiprocessing.get_context("spawn"),
            search_function=_spawned_search,
        )
    )[0]["site"] == "Example"


def test_missing_sherlock_dependency_is_actionable(monkeypatch):
    original_import = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "sherlock_project.notify":
            raise ModuleNotFoundError("No module named 'sherlock_project'")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)

    with pytest.raises(RuntimeError, match=r"pip install.*\[sherlock\]"):
        sherlock_integration._search("user")
