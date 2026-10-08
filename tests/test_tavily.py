"""Tests for the Tavily response mapping and HTTP error handling.

These are pure-function tests against the mappers plus a sanity check that
the bearer header is populated. The full HTTP path is exercised in the
integration tests against the real API.
"""
from __future__ import annotations

import httpx
import pytest

from tavily_search_mcp.tavily import (
    TavilyAuthError,
    TavilyClient,
    TavilyError,
    TavilyQuotaError,
    TavilyRateLimitError,
    _map_extract_response,
    _map_search_response,
    _parse_response,
)


# ---------------------------------------------------------------- mappers ---


def test_map_search_empty_payload():
    out = _map_search_response({}, 8000)
    assert out["query"] == ""
    assert out["answer"] == ""
    assert out["results"] == []
    assert out["request_id"] == ""


def test_map_search_drops_entries_without_url():
    payload = {
        "query": "rust",
        "results": [
            {"title": "Has URL", "url": "https://example.com", "content": "x"},
            {"title": "No URL", "content": "noise"},
            "not-a-dict",
        ],
        "request_id": "abc-123",
    }
    out = _map_search_response(payload, 8000)
    assert len(out["results"]) == 1
    assert out["results"][0]["title"] == "Has URL"
    assert out["request_id"] == "abc-123"


def test_map_search_null_raw_content_renders_as_empty_string():
    # Per the API contract, raw_content may be null even on `advanced` depth.
    payload = {
        "results": [{"title": "t", "url": "https://e.com", "content": "x", "raw_content": None}],
    }
    out = _map_search_response(payload, 8000)
    r = out["results"][0]
    assert r["raw_content"] == ""
    assert r["raw_content_chars"] == 0
    assert r["truncated"] is False


def test_map_search_truncates_long_raw_content():
    big = "x" * 12_000
    payload = {
        "results": [{"title": "Big", "url": "https://e.com", "content": "x", "raw_content": big}],
    }
    out = _map_search_response(payload, 8000)
    r = out["results"][0]
    assert r["raw_content"] != big
    assert len(r["raw_content"]) <= 8000
    assert r["raw_content_chars"] == 12_000
    assert r["raw_content_truncated_chars"] < 12_000
    assert r["truncated"] is True
    assert "[…truncated" in r["raw_content"]


def test_map_search_keeps_short_raw_content_verbatim():
    payload = {
        "results": [{"title": "Short", "url": "https://e.com", "content": "x", "raw_content": "hello"}],
    }
    out = _map_search_response(payload, 8000)
    r = out["results"][0]
    assert r["raw_content"] == "hello"
    assert r["raw_content_chars"] == 5
    assert r["truncated"] is False


def test_map_search_carries_published_date_when_present():
    payload = {
        "results": [{
            "title": "News",
            "url": "https://e.com",
            "content": "x",
            "published_date": "Tue, 11 Mar 2025 17:00:00 GMT",
            "favicon": "https://e.com/favicon.ico",
            "id": "a3f9c2-04",
        }],
    }
    out = _map_search_response(payload, 8000)
    r = out["results"][0]
    assert r["published_date"] == "Tue, 11 Mar 2025 17:00:00 GMT"
    assert r["favicon"] == "https://e.com/favicon.ico"
    assert r["id"] == "a3f9c2-04"


def test_map_search_answer_only_when_returned():
    payload = {"results": [], "answer": ""}
    out = _map_search_response(payload, 8000)
    assert out["answer"] == ""
    payload = {"results": [], "answer": "A short direct answer."}
    out = _map_search_response(payload, 8000)
    assert out["answer"] == "A short direct answer."


def test_map_extract_empty_payload():
    out = _map_extract_response({}, 8000)
    assert out["results"] == []
    assert out["failed_results"] == []


def test_map_extract_partial_success():
    payload = {
        "results": [{"url": "https://example.com/article", "raw_content": "Body"}],
        "failed_results": [{"url": "https://example.com/missing", "error": "Failed to retrieve content"}],
        "request_id": "req-1",
    }
    out = _map_extract_response(payload, 8000)
    assert len(out["results"]) == 1
    assert out["results"][0]["url"] == "https://example.com/article"
    assert out["results"][0]["raw_content"] == "Body"
    assert out["results"][0]["raw_content_chars"] == 4
    assert out["results"][0]["truncated"] is False
    assert len(out["failed_results"]) == 1
    assert out["failed_results"][0]["url"] == "https://example.com/missing"
    assert "Failed" in out["failed_results"][0]["error"]
    assert out["request_id"] == "req-1"


def test_map_extract_all_failures_is_a_real_outcome():
    """HTTP 200 can have all valid URLs in failed_results; both arrays
    must still come through cleanly."""
    payload = {
        "results": [],
        "failed_results": [
            {"url": "https://example.com/a", "error": "timeout"},
            {"url": "https://example.com/b", "error": "blocked"},
        ],
    }
    out = _map_extract_response(payload, 8000)
    assert out["results"] == []
    assert len(out["failed_results"]) == 2


def test_map_extract_truncates_long_raw_content():
    big = "x" * 9_000
    payload = {
        "results": [{"url": "https://e.com", "raw_content": big}],
        "failed_results": [],
    }
    out = _map_extract_response(payload, 8000)
    r = out["results"][0]
    assert len(r["raw_content"]) <= 8000
    assert r["raw_content_chars"] == 9_000
    assert r["raw_content_truncated_chars"] < 9_000
    assert r["truncated"] is True
    assert "[…truncated" in r["raw_content"]


def test_map_extract_carries_images_and_favicon():
    payload = {
        "results": [{
            "url": "https://e.com",
            "raw_content": "x",
            "images": ["https://e.com/i.png"],
            "favicon": "https://e.com/f.ico",
        }],
    }
    out = _map_extract_response(payload, 8000)
    assert out["results"][0]["images"] == ["https://e.com/i.png"]
    assert out["results"][0]["favicon"] == "https://e.com/f.ico"


# ------------------------------------------------------------------- errors --


def _make_response(status: int, body=None, text: str = "") -> httpx.Response:
    """Synthesize an httpx.Response with an attached request.

    httpx 0.28+ traces responses back to their originating request, so any
    synthetic Response without one throws when you ask for `.json()` or
    `.text`. We always supply both.
    """
    request = httpx.Request("POST", "https://api.tavily.com/test")
    if body is not None:
        import json as _json
        content = _json.dumps(body).encode("utf-8")
        return httpx.Response(status_code=status, content=content, request=request)
    return httpx.Response(status_code=status, text=text, request=request)


def test_parse_response_401_raises_auth_error():
    with pytest.raises(TavilyAuthError) as ei:
        _parse_response(_make_response(401, text="Unauthorized"))
    assert "401" in str(ei.value) or "Unauthorized" in str(ei.value)


def test_parse_response_429_raises_rate_limit():
    with pytest.raises(TavilyRateLimitError):
        _parse_response(_make_response(429, text="rate limited"))


def test_parse_response_432_raises_quota():
    with pytest.raises(TavilyQuotaError) as ei:
        _parse_response(_make_response(432, text="key limit"))
    assert "quota" in str(ei.value).lower() or "432" in str(ei.value)


def test_parse_response_433_raises_quota():
    with pytest.raises(TavilyQuotaError):
        _parse_response(_make_response(433, text="paygo"))


def test_parse_response_500_raises_generic():
    with pytest.raises(TavilyError) as ei:
        _parse_response(_make_response(500, text="internal"))
    assert "500" in str(ei.value)


def test_parse_response_200_non_json_raises():
    request = httpx.Request("POST", "https://api.tavily.com/test")
    resp = httpx.Response(status_code=200, content=b"<html>not json</html>", request=request)
    with pytest.raises(TavilyError) as ei:
        _parse_response(resp)
    assert "non-JSON" in str(ei.value)


def test_parse_response_200_json_returns_body():
    body = {"results": [], "request_id": "abc"}
    out = _parse_response(_make_response(200, body=body))
    assert out == body


# ----------------------------------------------------------------- headers --


def test_bearer_header_is_attached():
    """Per SPECS §4: auth is `Authorization: Bearer <key>`. The token never
    appears in the request body. This test uses a recording transport."""
    import asyncio

    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
        captured["body"] = request.content
        return httpx.Response(200, json={"results": [], "request_id": "x"}, request=request)

    transport = httpx.MockTransport(handler)
    client_obj = TavilyClient(api_key="tvly-secret-key-xyz")

    async def run():
        async with httpx.AsyncClient(transport=transport) as http:
            client_obj.attach(http)
            return await client_obj.search("hello", max_results=1)

    asyncio.run(run())

    assert captured["headers"]["authorization"] == "Bearer tvly-secret-key-xyz"
    # Token must NOT appear in the body.
    assert b"tvly-secret-key-xyz" not in captured["body"]
