"""Tavily v1 HTTP client and response mapping.

Per SPECS §4: a thin async httpx wrapper around `POST /search` and
`POST /extract`, with bearer auth via the TAVILY_API_KEY env var. The
mapping layer projects Tavily's raw JSON onto flat shapes the tools return
via `structured_content`. Per-result `raw_content` truncation is handled
here so each tool call materialises with `raw_content_chars` /
`raw_content_truncated_chars` already populated.

The server deliberately uses raw httpx instead of the official
`tavily-python` SDK to keep the dependency surface to one (httpx is already
pulled in by FastMCP's async runtime) and so the response mapping is fully
testable without SDK mocking. Behaviour matches the v1 API at
https://docs.tavily.com/documentation/api-reference/endpoint/search and
/extract.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.tavily.com"
USER_AGENT = "tavily-search-mcp/0.1"


class TavilyError(RuntimeError):
    """Raised on a Tavily transport or non-2xx HTTP error."""


class TavilyAuthError(TavilyError):
    """Raised when Tavily rejects the API key (HTTP 401)."""


class TavilyRateLimitError(TavilyError):
    """Raised when Tavily rate-limits (HTTP 429) or hits the Key/Plan quota (432/433)."""


class TavilyQuotaError(TavilyError):
    """Raised when Tavily returns 432/433 (`Key limit`/`PayGo limit exceeded`)."""


class TavilyClient:
    """Async HTTP client for the two Tavily endpoints this server uses."""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = 60.0,
        max_raw_chars: int = 8000,
    ) -> None:
        if not api_key:
            raise TavilyError("TAVILY_API_KEY is not set; tools will refuse to call Tavily")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._max_raw_chars = max_raw_chars
        self._client: httpx.AsyncClient | None = None

    def attach(self, client: httpx.AsyncClient) -> None:
        """Bind a shared httpx client; lifespan-managed."""
        self._client = client

    async def aclose(self) -> None:
        # No-op; the shared client is owned by the lifespan.
        self._client = None

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            raise TavilyError("Tavily client not attached to a shared httpx session")
        return self._client

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
    ) -> dict[str, Any]:
        """Run Tavily `POST /search`. Returns the mapped SPECS §3.1 shape."""
        body: dict[str, Any] = {
            "query": query,
            "max_results": max(1, min(int(max_results), 20)),
            "search_depth": search_depth,
            "topic": topic,
            "include_answer": include_answer,
            "include_raw_content": include_raw_content,
            "include_favicon": include_favicon,
            "chunks_per_source": max(1, min(int(chunks_per_source), 3)),
        }
        payload = await self._post("/search", body)
        return _map_search_response(payload, self._max_raw_chars)

    async def extract(
        self,
        urls: list[str],
        *,
        extract_depth: str = "basic",
        format: str = "markdown",
        include_favicon: bool = True,
    ) -> dict[str, Any]:
        """Run Tavily `POST /extract` for 1-20 URLs in one request.

        Returns the mapped SPECS §3.2 shape, including the per-URL
        `failed_results` array.
        """
        body: dict[str, Any] = {
            "urls": list(urls),
            "extract_depth": extract_depth,
            "format": format,
            "include_favicon": include_favicon,
        }
        payload = await self._post("/extract", body)
        return _map_extract_response(payload, self._max_raw_chars)

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = await self._http.post(
                self._base_url + path,
                json=body,
                headers=self._headers(),
                timeout=self._timeout_s,
            )
        except httpx.HTTPError as e:
            raise TavilyError(f"Tavily transport error: {e.__class__.__name__}: {e}") from e
        return _parse_response(resp)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }


# ----------------------------------------------------------------------- I/O --


def _parse_response(resp: httpx.Response) -> dict[str, Any]:
    """Validate an HTTP response and return the JSON body. Raises typed errors."""
    if resp.status_code == 401:
        raise TavilyAuthError("Tavily rejected the API key (401 Unauthorized)")
    if resp.status_code in (432, 433):
        # Tavily signals quota exhaustion with 432 (Key/Plan limit) or 433 (PayGo).
        raise TavilyQuotaError(
            f"Tavily quota exhausted (HTTP {resp.status_code}); check the dashboard for the active limit."
        )
    if resp.status_code == 429:
        raise TavilyRateLimitError("Tavily rate-limited this key (429 Too Many Requests)")
    if resp.status_code >= 400:
        text = (resp.text or "")[:200]
        raise TavilyError(f"Tavily upstream HTTP {resp.status_code}: {text!r}")
    try:
        body = resp.json()
    except ValueError as e:
        raise TavilyError(f"Tavily returned non-JSON body (status {resp.status_code}): {e}") from e
    return body


# ------------------------------------------------------------------ mappings --


def _map_search_response(payload: dict[str, Any], max_raw_chars: int) -> dict[str, Any]:
    """Project `/search` payload onto the SPECS §3.1 shape.

    `raw_content` may legitimately be null even when the call is `advanced` —
    treat it as empty string and keep the field shaped consistently. `answer`
    is omitted when Tavily didn't return one (the API contract).
    """
    results_raw = payload.get("results") or []
    results: list[dict[str, Any]] = []
    for entry in results_raw:
        if not isinstance(entry, dict):
            continue
        url = (entry.get("url") or "").strip()
        if not url:
            continue
        raw = entry.get("raw_content")
        raw_text = raw if isinstance(raw, str) else ""
        raw_chars = len(raw_text)
        truncated = raw_chars > max_raw_chars
        if truncated:
            raw_text = _truncate_raw(raw_text, max_raw_chars)
        results.append(
            {
                "title": entry.get("title") or "",
                "url": url,
                "content": entry.get("content") or "",
                "score": entry.get("score"),
                "raw_content": raw_text if raw else "",
                "raw_content_chars": raw_chars,
                "raw_content_truncated_chars": len(raw_text) if truncated else raw_chars,
                "truncated": truncated,
                "published_date": entry.get("published_date"),
                "favicon": entry.get("favicon"),
                "id": entry.get("id"),
            }
        )

    response = {
        "query": payload.get("query") or "",
        "answer": payload.get("answer") if isinstance(payload.get("answer"), str) else "",
        "results": results,
        "request_id": payload.get("request_id") or "",
        "response_time": payload.get("response_time"),
        "raw_result_count": len(results_raw),
    }
    return response


def _map_extract_response(payload: dict[str, Any], max_raw_chars: int) -> dict[str, Any]:
    """Project `/extract` payload onto the SPECS §3.2 shape.

    Per-URL failures live in `failed_results`; HTTP 200 can have all URLs in
    `failed_results` and an empty `results`. Both arrays are surfaced so the
    model can see exactly which URLs failed and why.
    """
    results_in = payload.get("results") or []
    results: list[dict[str, Any]] = []
    for entry in results_in:
        if not isinstance(entry, dict):
            continue
        url = (entry.get("url") or "").strip()
        if not url:
            continue
        raw = entry.get("raw_content")
        raw_text = raw if isinstance(raw, str) else ""
        raw_chars = len(raw_text)
        truncated = raw_chars > max_raw_chars
        if truncated:
            raw_text = _truncate_raw(raw_text, max_raw_chars)
        results.append(
            {
                "url": url,
                "raw_content": raw_text,
                "raw_content_chars": raw_chars,
                "raw_content_truncated_chars": len(raw_text) if truncated else raw_chars,
                "truncated": truncated,
                "images": entry.get("images") or [],
                "favicon": entry.get("favicon"),
            }
        )
    failed_in = payload.get("failed_results") or []
    failed = []
    for entry in failed_in:
        if not isinstance(entry, dict):
            continue
        failed.append(
            {
                "url": entry.get("url") or "",
                "error": entry.get("error") or "",
            }
        )

    return {
        "results": results,
        "failed_results": failed,
        "request_id": payload.get("request_id") or "",
        "response_time": payload.get("response_time"),
        "raw_result_count": len(results_in),
        "raw_failed_count": len(failed_in),
    }


# ----------------------------------------------------------------- helpers ---

_TRUNCATION_MARKER = "\n\n[…truncated to fit the context budget; call extract_url on the same URL with a narrower focus, or ask for a specific section…]"


def _truncate_raw(text: str, max_chars: int) -> str:
    """Slice to leave room for the marker, then append it.

    Python `str` slicing is char-safe so we never need to deal with partial
    multi-byte sequences. The marker is short and stable so the model can
    pattern-match on it.
    """
    room = max_chars - len(_TRUNCATION_MARKER)
    if room <= 0:
        return text[:max_chars]
    return text[:room] + _TRUNCATION_MARKER
