from tool_registry import TOOL_REGISTRY, get_supported_tools, get_tool_metadata


def test_supported_tools_are_curated_and_sorted():
    tools = get_supported_tools()
    assert tools == sorted(TOOL_REGISTRY)
    assert {"holehe", "sherlock", "maigret", "amass", "sublist3r"} <= set(tools)
    assert "dorker" not in tools


def test_tool_metadata_has_expected_fields_and_values():
    metadata = get_tool_metadata("holehe")
    assert metadata == TOOL_REGISTRY["holehe"]
    assert metadata["category"] == "email"
    assert metadata["repo"].startswith("https://")
    assert metadata["install_method"] == "pip"


def test_metadata_lookup_normalizes_name_and_handles_unknown_inputs():
    assert get_tool_metadata("  HOLEHE  ") == TOOL_REGISTRY["holehe"]
    assert get_tool_metadata("unknown-tool") is None
    assert get_tool_metadata(None) is None
