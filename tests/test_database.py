from tool_registry import get_supported_tools, get_tool_metadata


def test_supported_tools_are_curated():
    tools = get_supported_tools()
    assert "holehe" in tools
    assert "sherlock" in tools
    assert "maigret" in tools
    assert "amass" in tools
    assert "sublist3r" in tools
    assert "dorker" not in tools


def test_tool_metadata_is_valid():
    metadata = get_tool_metadata("holehe")
    assert metadata is not None
    assert metadata["category"] == "email"
    assert metadata["repo"].startswith("https://")

    missing = get_tool_metadata("unknown-tool")
    assert missing is None
