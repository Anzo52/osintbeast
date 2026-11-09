import io
import sys
import zipfile

import pytest
import requests

import downloader
import installer


def _zip_response(files):
    contents = io.BytesIO()
    with zipfile.ZipFile(contents, "w") as archive:
        for name, value in files.items():
            archive.writestr(name, value)

    class Response:
        content = contents.getvalue()

        def raise_for_status(self):
            pass

    return Response()


def test_download_tool_extracts_registered_repository(tmp_path, monkeypatch):
    response = _zip_response({"holehe-main/README.md": "project"})
    requests_seen = []

    def fake_get(url, timeout):
        requests_seen.append((url, timeout))
        return response

    monkeypatch.setattr(downloader.requests, "get", fake_get)

    target = downloader.download_tool(" HOLEHE ", str(tmp_path))

    assert target == str(tmp_path / "holehe")
    assert (tmp_path / "holehe" / "holehe-main" / "README.md").read_text() == "project"
    assert requests_seen == [
        ("https://github.com/megadose/holehe/archive/refs/heads/main.zip", 30)
    ]


def test_download_repo_tries_master_after_main_fails(tmp_path, monkeypatch):
    response = _zip_response({"repo-master/README.md": "project"})
    requests_seen = []

    class NotFoundResponse:
        content = b""

        def raise_for_status(self):
            raise requests.HTTPError("not found")

    responses = [NotFoundResponse(), response]

    def fake_get(url, timeout):
        requests_seen.append(url)
        return responses.pop(0)

    monkeypatch.setattr(downloader.requests, "get", fake_get)

    result = downloader.download_github_repo(
        "https://github.com/example/repo", str(tmp_path)
    )

    assert result == str(tmp_path.resolve())
    assert requests_seen == [
        "https://github.com/example/repo/archive/refs/heads/main.zip",
        "https://github.com/example/repo/archive/refs/heads/master.zip",
    ]


def test_download_repo_rejects_archive_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(
        downloader.requests, "get", lambda *args, **kwargs: _zip_response(
            {"../outside.txt": "not written"}
        )
    )

    with pytest.raises(ValueError, match="unsafe path"):
        downloader.download_github_repo(
            "https://github.com/example/repo", str(tmp_path / "download")
        )

    assert not (tmp_path / "outside.txt").exists()


def test_install_tool_installs_project_nested_in_download_directory(
    tmp_path, monkeypatch
):
    project = tmp_path / "tools" / "holehe" / "holehe-main"
    project.mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname = 'holehe'\n")
    calls = []
    monkeypatch.setattr(installer.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)))

    installer.install_tool("HOLEHE", str(tmp_path / "tools" / "holehe"))

    assert calls == [
        (
            ([sys.executable, "-m", "pip", "install", str(project)],),
            {"check": True, "timeout": 600},
        )
    ]


def test_install_tool_rejects_unknown_and_non_python_tools(tmp_path):
    with pytest.raises(ValueError, match="not supported"):
        installer.install_tool("unknown", str(tmp_path))

    with pytest.raises(ValueError, match="not supported.*binary"):
        installer.install_tool("amass", str(tmp_path))


def test_install_tool_reports_missing_python_project(tmp_path):
    with pytest.raises(ValueError, match="No Python project metadata"):
        installer.install_tool("holehe", str(tmp_path))
