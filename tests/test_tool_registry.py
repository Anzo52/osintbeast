from importlib.metadata import PackageNotFoundError

import pytest

import tool_registry
from tool_registry import TOOL_REGISTRY, get_supported_tools, get_tool_metadata


def test_supported_tools_are_curated_and_sorted():
    tools = get_supported_tools()
    assert tools == sorted(TOOL_REGISTRY)
    assert {"holehe", "sherlock", "maigret", "helix", "amass", "sublist3r"} <= set(tools)
    assert "dorker" not in tools


def test_tool_metadata_has_expected_fields_and_values():
    metadata = get_tool_metadata("holehe")
    assert metadata == TOOL_REGISTRY["holehe"]
    assert metadata["category"] == "email"
    assert metadata["repo"].startswith("https://")
    assert metadata["install_method"] == "pip"
    assert metadata["input_mechanism"] == "in-process Python API"
    assert {
        name: TOOL_REGISTRY[name]["input_mechanism"]
        for name in ("holehe", "maigret", "helix", "sherlock")
    } == {
        "holehe": "in-process Python API",
        "maigret": "in-process Python API",
        "helix": "in-process Python API",
        "sherlock": "in-process Python API",
    }


def test_unimplemented_tools_require_private_input_when_integrated():
    assert TOOL_REGISTRY["sublist3r"]["input_mechanism"] == (
        "not integrated; future runs require stdin/private pipe"
    )
    assert TOOL_REGISTRY["amass"]["input_mechanism"] == (
        "not integrated; future runs require stdin/private pipe"
    )


def test_metadata_lookup_normalizes_name_and_handles_unknown_inputs():
    assert get_tool_metadata("  HOLEHE  ") == TOOL_REGISTRY["holehe"]
    assert get_tool_metadata("unknown-tool") is None
    assert get_tool_metadata(None) is None


@pytest.mark.parametrize(
    ("name", "target"),
    [
        ("holehe", "holehe"),
        ("sherlock", "sherlock-project"),
        ("maigret", "maigret"),
        ("sublist3r", "Sublist3r"),
        ("helix", "helix-osint"),
    ],
)
def test_python_install_status_uses_distribution_name(monkeypatch, name, target):
    seen = []

    def find_distribution(distribution_name):
        seen.append(distribution_name)
        if distribution_name == target:
            return object()
        raise PackageNotFoundError(distribution_name)

    monkeypatch.setattr(tool_registry, "distribution", find_distribution)
    assert tool_registry.is_tool_installed(f" {name.upper()} ")
    assert seen == [target]

    def missing(distribution_name):
        raise PackageNotFoundError(distribution_name)

    monkeypatch.setattr(tool_registry, "distribution", missing)
    assert not tool_registry.is_tool_installed(name)


def test_binary_install_status_checks_path(monkeypatch):
    monkeypatch.setattr(
        tool_registry, "which", lambda name: "/bin/amass" if name == "amass" else None
    )
    assert tool_registry.is_tool_installed("amass")
    monkeypatch.setattr(tool_registry, "which", lambda name: None)
    assert not tool_registry.is_tool_installed("amass")


def test_install_status_rejects_unknown_tool():
    with pytest.raises(ValueError, match="Unknown tool"):
        tool_registry.is_tool_installed("unknown")


def test_install_status_does_not_hide_unexpected_errors(monkeypatch):
    def fail(_name):
        raise OSError("metadata unavailable")

    monkeypatch.setattr(tool_registry, "distribution", fail)
    with pytest.raises(OSError, match="metadata unavailable"):
        tool_registry.is_tool_installed("holehe")
