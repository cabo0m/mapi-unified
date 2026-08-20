from __future__ import annotations

from mapi_platform.network import mcp_connection_urls


def test_mcp_connection_urls_are_platform_neutral() -> None:
    assert mcp_connection_urls(public_origin=None, port=8015) == {
        "loopback_mcp_url": "http://127.0.0.1:8015/mcp/",
        "public_mcp_url": None,
        "recommended_mcp_url": "http://127.0.0.1:8015/mcp/",
    }
    public = mcp_connection_urls(public_origin="https://example.test/", port=8015)
    assert public["public_mcp_url"] == "https://example.test/mcp/"
    assert public["recommended_mcp_url"] == "https://example.test/mcp/"
