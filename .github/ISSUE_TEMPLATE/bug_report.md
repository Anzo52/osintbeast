import os

from downloader import download_tool
from installer import install_tool


class DummyResponse:
    def __init__(self, payload):
        self.content = payload

    def raise_for_status(self):
        return None


def test_download_tool_uses_registry(monkeypatch, tmp_path):
    calls = {}

    def fake_download_github_repo(repo_url, output_dir):
        calls["repo"] = repo_url
        calls["output_dir"] = output_dir
        os.makedirs(output_dir, exist_ok=True)
        return output_dir

    monkeypatch.setattr("downloader.download_github_repo", fake_download_github_repo)

    target_dir = download_tool("holehe", output_dir=str(tmp_path))

    assert target_dir == os.path.join(str(tmp_path), "holehe")
    assert calls["repo"] == "https://github.com/megadose/holehe"


def test_install_tool_supports_requirements(tmp_path, monkeypatch):
    tool_dir = tmp_path / "holehe"
    tool_dir.mkdir()
    (tool_dir / "requirements.txt").write_text("requests\n")

    captured = {}

    def fake_install_requirements(path):
        captured["path"] = path

    monkeypatch.setattr("installer.install_requirements", fake_install_requirements)

    result = install_tool("holehe", str(tool_dir))

    assert result == str(tool_dir)
    assert captured["path"] == str(tool_dir / "requirements.txt")

