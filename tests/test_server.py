"""In-process tests for the FastMCP server.

These run against the in-memory FastMCP instance via `fastmcp.Client`. The
outbound Tavily call is mocked at the `TavilyClient.search` and
`TavilyClient.extract` methods (per the start-up-prompt's "mock at the
function boundary, not at the lifespan" rule). The real network is exercised
separately in `test_server_integration.py` (skipped unless
`TAVILY_NETWORK_TESTS=1`).
"""
from __future__ import annotations

import pytest
from fastmcp import Client

import tavily_search_mcp.tavily as tavily_module
import tavily_search_mcp.server as server_module
from tavily_search_mcp.server import EMPTY_EXTRACT_NOTE, EMPTY_SEARCH_NOTE, mcp


class FakeTavily:
    """Pluggable canned responses keyed by query / urls.

    Wire into the running server by patching the methods on
    `TavilyClient` (see the `fake_tavily` fixture). Records every call for
    the assertions in each test.
    """

    def __init__(self) -> None:
        self.search_responses: dict[str, dict] = {}
        self.extract_responses: dict[tuple, dict] = {}
        self.search_exceptions: dict[str, BaseException] = {}
        self.extract_exceptions: dict[tuple, BaseException] = {}
        self.search_calls: list[tuple[str, int]] = []
        self.extract_calls: list[tuple[tuple[str, ...], int]] = []

    def set_search(self, query: str, payload: dict) -> None:
        self.search_responses[query] = payload

    def set_extract(self, urls: list[str], payload: dict) -> None:
        self.extract_responses[tuple(urls)] = payload

    def set_search_error(self, query: str, exc: BaseException) -> None:
        self.search_exceptions[query] = exc

    def set_extract_error(self, urls: list[str], exc: BaseException) -> None:
        self.extract_exceptions[tuple(urls)] = exc

    async def search(
        self,
        query: str,
        *,
        max_results: int = 5,
        search_depth: str = "advanced",
        topic: str = "general",
        include_answer: bool | str = "basic",
        include_raw_content: bool | str = "markdown",
        include_favicon: bool = True,
        chunks_per_source: int = 3,
    ) -> dict:
        self.search_calls.append((query, max_results))
        if query in self.search_exceptions:
            raise self.search_exceptions[query]
        return self.search_responses.get(query, {
            "query": query, "answer": "", "results": [],
            "request_id": "", "response_time": 0.0, "raw_result_count": 0,
        })

    async def extract(
        self,
        urls: list[str],
        *,
        extract_depth: str = "basic",
        format: str = "markdown",
        include_favicon: bool = True,
    ) -> dict:
        key = tuple(urls)
        self.extract_calls.append((key, len(urls)))
        if key in self.extract_exceptions:
            raise self.extract_exceptions[key]
        return self.extract_responses.get(key, {
            "results": [], "failed_results": [], "request_id": "",
            "response_time": 0.0, "raw_result_count": 0, "raw_failed_count": 0,
        })


@pytest.fixture(autouse=True)
def _fresh_cache_per_test(monkeypatch):
    """test_server.py additionally pins a fake API key and silences logs.

    Cache dir comes from the autouse `_per_test_cache` fixture in conftest.
    """
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test-key-for-unit-tests")
    monkeypatch.setenv("MCP_LOG_LEVEL", "ERROR")
    yield


@pytest.fixture
def fake_tavily(monkeypatch):
    """Replace the TavilyClient methods with a controllable fake.

    Class methods are patched so the lifespan's TavilyClient instance routes
    every call to the fake. This is the fixture-mock pattern from the
    start-up prompt: mock the outbound function boundary, NOT the lifespan.
    """
    fake = FakeTavily()

    async def _search(self, query: str, **kwargs) -> dict:
        return await fake.search(query, **kwargs)

    async def _extract(self, urls: list[str], **kwargs) -> dict:
        return await fake.extract(urls, **kwargs)

    monkeypatch.setattr(tavily_module.TavilyClient, "search", _search)
    monkeypatch.setattr(tavily_module.TavilyClient, "extract", _extract)
    return fake


# ---------------------------------------------------------------- tests -----


async def test_tools_list_advertises_two_tools(fake_tavily):
    async with Client(mcp) as c:
        tools = await c.list_tools()
        names = sorted(t.name for t in tools)
        assert names == ["extract_url", "web_search"]


async def test_prompts_list_advertises_two_prompts(fake_tavily):
    async with Client(mcp) as c:
        prompts = await c.list_prompts()
        names = sorted(p.name for p in prompts)
        assert names == ["fetch_document", "research_topic"]


async def test_research_topic_prompt_returns_user_message(fake_tavily):
    async with Client(mcp) as c:
        p = await c.get_prompt("research_topic", {"topic": "PostgreSQL"})
        assert p.messages
        text = p.messages[0].content.text
        assert "PostgreSQL" in text
        assert "canonical topic name" in text


async def test_fetch_document_prompt_returns_user_message(fake_tavily):
    async with Client(mcp) as c:
        p = await c.get_prompt("fetch_document", {
            "urls": ["https://example.com/docs"],
            "focus": "the public schema introspection API",
        })
        text = p.messages[0].content.text
        assert "https://example.com/docs" in text
        assert "public schema introspection API" in text


async def test_basic_search_returns_structured_results(fake_tavily):
    fake_tavily.set_search("Rust programming language", {
        "query": "Rust programming language",
        "answer": "A systems language",
        "results": [
            {
                "title": "Rust (programming language)", "url": "https://example.com/rust",
                "content": "Rust is a systems language.", "score": 0.9,
                "raw_content": "Raw body.", "raw_content_chars": 9,
                "raw_content_truncated_chars": 9, "truncated": False,
                "published_date": None, "favicon": None, "id": "abc",
            },
        ],
        "request_id": "req-1", "response_time": 0.5, "raw_result_count": 1,
    })
    async with Client(mcp) as c:
        result = await c.call_tool("web_search", {"query": "Rust programming language"})
        sc = result.structured_content
        assert sc["query"] == "Rust programming language"
        assert sc["answer"] == "A systems language"
        assert sc["cache"] == "miss"
        assert sc["results"][0]["title"] == "Rust (programming language)"
        assert sc["results"][0]["url"].startswith("https://example.com/rust")
        assert sc["request_id"] == "req-1"
        assert sc["usage"]["calls_this_session"] == 1


async def test_query_is_trimmed_and_echoed(fake_tavily):
    fake_tavily.set_search("hello", {
        "query": "hello", "answer": "", "results": [],
        "request_id": "", "response_time": 0.0, "raw_result_count": 0,
    })
    async with Client(mcp) as c:
        result = await c.call_tool("web_search", {"query": "  hello  "})
        assert result.structured_content["query"] == "hello"
        # Single underlying search call to the (faked) upstream.
        assert any(q == "hello" for q, _ in fake_tavily.search_calls)
        assert len(fake_tavily.search_calls) == 1


async def test_max_results_is_clamped_to_1_through_10(fake_tavily):
    many = [
        {
            "title": f"r{i}", "url": f"https://example.com/{i}",
            "content": "x", "score": 0.5, "raw_content": "",
            "raw_content_chars": 0, "raw_content_truncated_chars": 0,
            "truncated": False, "published_date": None, "favicon": None,
            "id": f"id{i}",
        }
        for i in range(12)
    ]
    payload = {
        "query": "anything", "answer": "", "results": many,
        "request_id": "r", "response_time": 0.0, "raw_result_count": len(many),
    }
    fake_tavily.set_search("anything", payload)
    async with Client(mcp) as c:
        r1 = await c.call_tool("web_search", {"query": "anything", "max_results": 0})
        assert len(r1.structured_content["results"]) == 1
        r2 = await c.call_tool("web_search", {"query": "anything", "max_results": 11})
        assert len(r2.structured_content["results"]) == 10
        r3 = await c.call_tool("web_search", {"query": "anything"})
        assert len(r3.structured_content["results"]) == 5


async def test_empty_search_returns_empty_with_note(fake_tavily):
    fake_tavily.set_search("how do I write async code in Rust as a beginner with tokio 2026", {
        "query": "anything", "answer": "", "results": [],
        "request_id": "", "response_time": 0.0, "raw_result_count": 0,
    })
    async with Client(mcp) as c:
        result = await c.call_tool(
            "web_search",
            {"query": "how do I write async code in Rust as a beginner with tokio 2026"},
        )
        sc = result.structured_content
        assert sc["results"] == []
        assert sc["note"] == EMPTY_SEARCH_NOTE


async def test_second_call_within_ttl_serves_from_cache(fake_tavily):
    fake_tavily.set_search("cached query", {
        "query": "cached query", "answer": "", "results": [
            {
                "title": "Cached", "url": "https://example.com/c",
                "content": "Body", "score": 0.5, "raw_content": "",
                "raw_content_chars": 0, "raw_content_truncated_chars": 0,
                "truncated": False, "published_date": None, "favicon": None,
                "id": "cached-id",
            },
        ],
        "request_id": "r", "response_time": 0.0, "raw_result_count": 1,
    })
    async with Client(mcp) as c:
        r1 = await c.call_tool("web_search", {"query": "cached query"})
        r2 = await c.call_tool("web_search", {"query": "cached query"})
        assert r1.structured_content["cache"] == "miss"
        assert r2.structured_content["cache"] == "hit"
        # Only the first call hit the (faked) upstream; the second hit cache.
        upstream_hits = [(q, n) for q, n in fake_tavily.search_calls if q == "cached query"]
        assert len(upstream_hits) == 1
        # Cache hits do NOT increment the call counter.
        assert r2.structured_content["usage"]["calls_this_session"] == 1


async def test_force_refresh_bypasses_cache(fake_tavily):
    fake_tavily.set_search("fresh", {
        "query": "fresh", "answer": "", "results": [{
            "title": "R1", "url": "https://example.com/a",
            "content": "m1", "score": 0.7, "raw_content": "",
            "raw_content_chars": 0, "raw_content_truncated_chars": 0,
            "truncated": False, "published_date": None, "favicon": None,
            "id": "x",
        }],
        "request_id": "r", "response_time": 0.0, "raw_result_count": 1,
    })
    async with Client(mcp) as c:
        r1 = await c.call_tool("web_search", {"query": "fresh"})
        r2 = await c.call_tool("web_search", {"query": "fresh", "force_refresh": True})
        assert r1.structured_content["cache"] == "miss"
        assert r2.structured_content["cache"] == "bypassed"
        # Two upstream hits — bypass goes around the cache.
        assert sum(1 for q, _ in fake_tavily.search_calls if q == "fresh") == 2


async def test_search_invalid_query_returns_empty_with_note(fake_tavily):
    async with Client(mcp) as c:
        result = await c.call_tool("web_search", {"query": "   "})
        sc = result.structured_content
        assert sc["query"] == ""
        assert sc["results"] == []
        assert sc["note"] == EMPTY_SEARCH_NOTE
        # No upstream call was made.
        assert fake_tavily.search_calls == []


async def test_search_missing_api_key_returns_tool_error(fake_tavily, monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    async with Client(mcp) as c:
        with pytest.raises(Exception) as ei:
            await c.call_tool("web_search", {"query": "anything"})
        assert "TAVILY_API_KEY" in str(ei.value)


async def test_search_auth_error_returns_tool_error(fake_tavily):
    from tavily_search_mcp.tavily import TavilyAuthError
    fake_tavily.set_search_error("anything", TavilyAuthError("401"))
    async with Client(mcp) as c:
        with pytest.raises(Exception) as ei:
            await c.call_tool("web_search", {"query": "anything"})
        assert "rejected" in str(ei.value).lower()


async def test_search_rate_limit_error_returns_tool_error(fake_tavily):
    from tavily_search_mcp.tavily import TavilyRateLimitError
    fake_tavily.set_search_error("anything", TavilyRateLimitError("429"))
    async with Client(mcp) as c:
        with pytest.raises(Exception) as ei:
            await c.call_tool("web_search", {"query": "anything"})
        assert "rate" in str(ei.value).lower()


async def test_search_quota_error_returns_tool_error(fake_tavily):
    from tavily_search_mcp.tavily import TavilyQuotaError
    fake_tavily.set_search_error("anything", TavilyQuotaError("432"))
    async with Client(mcp) as c:
        with pytest.raises(Exception) as ei:
            await c.call_tool("web_search", {"query": "anything"})
        assert "quota" in str(ei.value).lower() or "432" in str(ei.value)


async def test_search_generic_error_returns_tool_error(fake_tavily):
    from tavily_search_mcp.tavily import TavilyError
    fake_tavily.set_search_error("anything", TavilyError("boom"))
    async with Client(mcp) as c:
        with pytest.raises(Exception) as ei:
            await c.call_tool("web_search", {"query": "anything"})
        assert "upstream error" in str(ei.value).lower()


# -------------------------------------------------------------- extract_url --


async def test_extract_url_single_returns_results(fake_tavily):
    fake_tavily.set_extract(["https://example.com/docs"], {
        "results": [{
            "url": "https://example.com/docs",
            "raw_content": "Body", "raw_content_chars": 4,
            "raw_content_truncated_chars": 4, "truncated": False,
            "images": [], "favicon": "https://example.com/f.ico",
        }],
        "failed_results": [],
        "request_id": "req-1", "response_time": 0.5, "raw_result_count": 1, "raw_failed_count": 0,
    })
    async with Client(mcp) as c:
        result = await c.call_tool("extract_url", {"urls": ["https://example.com/docs"]})
        sc = result.structured_content
        assert sc["urls"] == ["https://example.com/docs"]
        assert sc["cache"] == "miss"
        assert sc["results"][0]["raw_content"] == "Body"
        assert sc["results"][0]["favicon"] == "https://example.com/f.ico"
        assert sc["failed_results"] == []
        assert sc["usage"]["calls_this_session"] == 1


async def test_extract_url_batch_partial_failure(fake_tavily):
    """When one URL fails, it's surfaced in `failed_results` — not dropped."""
    fake_tavily.set_extract(
        ["https://example.com/a", "https://example.com/b"],
        {
            "results": [{
                "url": "https://example.com/a",
                "raw_content": "Body A", "raw_content_chars": 6,
                "raw_content_truncated_chars": 6, "truncated": False,
                "images": [], "favicon": None,
            }],
            "failed_results": [
                {"url": "https://example.com/b", "error": "Failed to retrieve content"},
            ],
            "request_id": "r2", "response_time": 1.0,
            "raw_result_count": 1, "raw_failed_count": 1,
        },
    )
    async with Client(mcp) as c:
        result = await c.call_tool(
            "extract_url",
            {"urls": ["https://example.com/a", "https://example.com/b"]},
        )
        sc = result.structured_content
        assert len(sc["results"]) == 1
        assert sc["results"][0]["url"] == "https://example.com/a"
        assert sc["results"][0]["raw_content"] == "Body A"
        # The failure URL appears with an `error` field; not dropped silently.
        assert len(sc["failed_results"]) == 1
        assert sc["failed_results"][0]["url"] == "https://example.com/b"
        assert "Failed" in sc["failed_results"][0]["error"]
        assert sc["request_id"] == "r2"


async def test_extract_url_all_failures(fake_tavily):
    fake_tavily.set_extract(["https://example.com/missing"], {
        "results": [],
        "failed_results": [
            {"url": "https://example.com/missing", "error": "Page behind a paywall"},
        ],
        "request_id": "r3", "response_time": 0.0,
        "raw_result_count": 0, "raw_failed_count": 1,
    })
    async with Client(mcp) as c:
        result = await c.call_tool("extract_url", {"urls": ["https://example.com/missing"]})
        sc = result.structured_content
        assert sc["results"] == []
        assert len(sc["failed_results"]) == 1
        assert "paywall" in sc["failed_results"][0]["error"]
        assert sc["note"] == EMPTY_EXTRACT_NOTE


async def test_extract_url_second_call_hits_cache(fake_tavily):
    fake_tavily.set_extract(["https://example.com/cached"], {
        "results": [{
            "url": "https://example.com/cached",
            "raw_content": "Cached body", "raw_content_chars": 11,
            "raw_content_truncated_chars": 11, "truncated": False,
            "images": [], "favicon": None,
        }],
        "failed_results": [], "request_id": "rc",
        "response_time": 0.0, "raw_result_count": 1, "raw_failed_count": 0,
    })
    async with Client(mcp) as c:
        r1 = await c.call_tool("extract_url", {"urls": ["https://example.com/cached"]})
        r2 = await c.call_tool("extract_url", {"urls": ["https://example.com/cached"]})
        assert r1.structured_content["cache"] == "miss"
        assert r2.structured_content["cache"] == "hit"
        # Cache hits do NOT increment the call counter.
        assert r2.structured_content["usage"]["calls_this_session"] == 1


async def test_extract_url_batch_is_one_call(fake_tavily):
    fake_tavily.set_extract(
        ["https://example.com/a", "https://example.com/b", "https://example.com/c"],
        {
            "results": [
                {"url": u, "raw_content": f"Body {i}",
                 "raw_content_chars": 6, "raw_content_truncated_chars": 6,
                 "truncated": False, "images": [], "favicon": None}
                for i, u in enumerate(["https://example.com/a", "https://example.com/b", "https://example.com/c"], 1)
            ],
            "failed_results": [], "request_id": "rbatch",
            "response_time": 0.0, "raw_result_count": 3, "raw_failed_count": 0,
        },
    )
    async with Client(mcp) as c:
        result = await c.call_tool(
            "extract_url",
            {"urls": ["https://example.com/a", "https://example.com/b", "https://example.com/c"]},
        )
        sc = result.structured_content
        assert len(sc["results"]) == 3
        # All three URLs go in a single batch — one upstream call.
        assert len(fake_tavily.extract_calls) == 1
        # And one increment on the counter.
        assert sc["usage"]["calls_this_session"] == 1


async def test_extract_url_rejects_non_http(fake_tavily):
    async with Client(mcp) as c:
        with pytest.raises(Exception) as ei:
            await c.call_tool("extract_url", {"urls": ["not-a-url"]})
        assert "http" in str(ei.value).lower()


async def test_extract_url_rejects_empty(fake_tavily):
    async with Client(mcp) as c:
        with pytest.raises(Exception) as ei:
            await c.call_tool("extract_url", {"urls": []})
    msg = str(ei.value).lower()
    # Pydantic 2.x raises a "too_short" error before our manual checks run;
    # we accept either path so the test stays stable across FastMCP versions.
    assert any(s in msg for s in ("at least 1", "at least one", "min_length", "too_short"))


async def test_extract_url_rejects_more_than_20(fake_tavily):
    async with Client(mcp) as c:
        with pytest.raises(Exception) as ei:
            await c.call_tool(
                "extract_url",
                {"urls": [f"https://example.com/{i}" for i in range(21)]},
            )
        assert "20" in str(ei.value)


async def test_extract_url_missing_api_key_returns_tool_error(fake_tavily, monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    async with Client(mcp) as c:
        with pytest.raises(Exception) as ei:
            await c.call_tool("extract_url", {"urls": ["https://example.com/x"]})
        assert "TAVILY_API_KEY" in str(ei.value)


# ----------------------------------------------------------------- protocol --


async def test_modern_protocol_negotiated(fake_tavily):
    async with Client(mcp) as c:
        assert c.protocol_version == "2026-07-28"


async def test_legacy_era_also_calls_the_tool(fake_tavily):
    fake_tavily.set_search("Rust", {
        "query": "Rust", "answer": "", "results": [],
        "request_id": "", "response_time": 0.0, "raw_result_count": 0,
    })
    async with Client(mcp, mode="legacy") as c:
        result = await c.call_tool("web_search", {"query": "Rust", "max_results": 3})
        sc = result.structured_content
        assert sc["query"] == "Rust"
        assert sc["note"] == EMPTY_SEARCH_NOTE


async def test_legacy_era_lists_tools(fake_tavily):
    async with Client(mcp, mode="legacy") as c:
        tools = await c.list_tools()
        names = sorted(t.name for t in tools)
        assert names == ["extract_url", "web_search"]
