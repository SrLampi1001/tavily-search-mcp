"""Integration tests: real Tavily calls, gated on opt-in.

Per SPECS §15. Network tests are opt-in via `TAVILY_NETWORK_TESTS=1`. Skipped
by default so CI stays hermetic.

These tests use the real `TAVILY_API_KEY` from `.env` (or the env) and the
real `api.tavily.com` endpoint. Set
`TAVILY_NETWORK_TESTS=1 .venv/bin/pytest tests/test_server_integration.py`
to run them.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastmcp import Client

try:
    from dotenv import load_dotenv
    if Path(".env").is_file():
        load_dotenv(".env", override=False)
except Exception:
    pass

from tavily_search_mcp.server import mcp

NETWORK_ENABLED = os.environ.get("TAVILY_NETWORK_TESTS") == "1"
pytestmark = pytest.mark.skipif(
    not NETWORK_ENABLED,
    reason="network tests disabled (set TAVILY_NETWORK_TESTS=1 to run)",
)


async def test_web_search_returns_results():
    async with Client(mcp) as c:
        r = await c.call_tool("web_search", {"query": "HTTP 418 status code", "max_results": 3})
        sc = r.structured_content
        assert sc["cache"] in {"miss", "hit", "bypassed"}
        assert len(sc["results"]) >= 1
        assert sc["usage"]["calls_this_session"] >= 1


async def test_web_search_carries_answer_when_enabled():
    async with Client(mcp) as c:
        r = await c.call_tool(
            "web_search",
            {"query": "HTTP 418 status code", "max_results": 2},
        )
        sc = r.structured_content
        # The server's default `include_answer=basic` should produce a non-empty
        # `answer` on a factual question. If Tavily returns nothing, skip.
        if not sc.get("answer"):
            pytest.skip("Tavily returned no answer for this query")
        assert isinstance(sc["answer"], str)
        assert len(sc["answer"]) > 0


async def test_extract_url_returns_results_against_known_pages():
    async with Client(mcp) as c:
        r = await c.call_tool(
            "extract_url",
            {"urls": ["https://example.com"]},
        )
        sc = r.structured_content
        assert sc["urls"] == ["https://example.com"]
        # Either the body is non-empty (success), or it's in failed_results.
        if sc["results"]:
            assert sc["results"][0]["raw_content_chars"] >= 0
        else:
            assert any(fr["url"] == "https://example.com" for fr in sc["failed_results"])
        assert sc["usage"]["calls_this_session"] >= 1


async def test_second_search_within_ttl_serves_from_cache():
    query = "HTTP 429 status code"
    async with Client(mcp) as c:
        r1 = await c.call_tool("web_search", {"query": query, "max_results": 2})
        r2 = await c.call_tool("web_search", {"query": query, "max_results": 2})
        assert r1.structured_content["cache"] in {"miss", "hit", "bypassed"}
        assert r2.structured_content["cache"] == "hit"
        # Cache hit must NOT increment the call counter.
        assert r2.structured_content["usage"]["calls_this_session"] == r1.structured_content["usage"]["calls_this_session"]
