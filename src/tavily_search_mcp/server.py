"""FastMCP server definition: tools + prompts.

Two tools (`web_search`, `extract_url`) over the Tavily v1 API, plus two
prompts (`research_topic`, `fetch_document`). Built on FastMCP 4.0.11 against
MCP 2026-07-28. Dual-era; serves both Streamable HTTP and stdio. Application
state (httpx client, Tavily client, on-disk cache, rate limiter, in-process
call counter) is owned by the FastMCP `lifespan`, so it's safely constructed
at startup and closed at shutdown, and the tools read it from
`ctx.request_context.lifespan_context`.
"""
from __future__ import annotations

import logging
import urllib.parse
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from .cache import DiskCache
from .call_counter import CallCounter
from .prompts import register_prompts
from .rate_limit import MinIntervalLimiter
from .settings import Settings, clamp_count, configure_logging
from .tavily import (
    TavilyAuthError,
    TavilyClient,
    TavilyError,
    TavilyQuotaError,
    TavilyRateLimitError,
)
from .normalize import canonical_batch_key

log = logging.getLogger(__name__)


INSTRUCTIONS = (
    "Two tools: web_search (Tavily /search with LLM-friendly snippets and "
    "optional raw_content) and extract_url (Tavily /extract for 1-20 URLs in "
    "one call). Two prompts: research_topic (plans one web_search), "
    "fetch_document (plans one extract_url). Tavily charges credits per call; "
    "every response includes usage.calls_this_session so the model can "
    "self-budget. Empty web_search results are normal for very long-tail "
    "queries — the tool returns a guidance note instead. Per-result "
    "raw_content is capped at ~8k chars; the cut is visible via "
    "raw_content_chars / raw_content_truncated_chars and truncated: true."
)


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncGenerator[dict, None]:
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    cache = DiskCache(settings.cache_dir, settings.cache_ttl_secs)
    rate_limiter = MinIntervalLimiter(settings.min_interval_ms)
    call_counter = CallCounter()
    tavily: TavilyClient | None = None
    if settings.api_key_set:
        tavily = TavilyClient(
            api_key=settings.api_key,
            base_url=settings.tavily_base_url,
            timeout_s=settings.http_timeout_s,
            max_raw_chars=settings.max_raw_chars,
        )
    else:
        log.warning("TAVILY_API_KEY is not set; tools will return a clear error on first call")

    async with httpx.AsyncClient(
        headers={"User-Agent": "tavily-search-mcp/0.1 (+https://github.com/)"},
        follow_redirects=True,
    ) as http_client:
        if tavily is not None:
            tavily.attach(http_client)
        yield {
            "settings": settings,
            "cache": cache,
            "rate_limiter": rate_limiter,
            "call_counter": call_counter,
            "tavily": tavily,
            "http_client": http_client,
        }


mcp = FastMCP(
    name="tavily-search-mcp",
    instructions=INSTRUCTIONS,
    cache_ttl=300,        # tool/prompt lists rarely change; safe to share publicly
    cache_scope="public",
    lifespan=lifespan,
)

register_prompts(mcp)


# ---------------------------------------------------------------------- tools --


EMPTY_SEARCH_NOTE = (
    "Tavily returned no results for this query. Rephrase once as a specific "
    "topic name with concrete terms (e.g. 'FastMCP lifespan context' instead "
    "of 'how do I keep state in MCP tools'), and if that also comes back "
    "empty, answer from your own knowledge and say so."
)

EMPTY_EXTRACT_NOTE = (
    "Tavily returned no extracted bodies for these URLs. They may be "
    "paywalled, behind JS shells, or unsupported; the failed_results array "
    "carries the per-URL error. Pick alternate URLs or summarise whatever "
    "metadata the response returns."
)


@mcp.tool(
    name="web_search",
    description=(
        "Web search via Tavily. Reach for this when the question is question-"
        "shaped, long-tail, or generally web-shaped and you want clean, LLM-"
        "ready snippets (not just links). Tavily charges credits per call "
        "(the signup tier is 1,000 credits/month; `advanced` depth is 2 "
        "credits, `basic`/`fast`/`ultra-fast` are 1); every response "
        "includes `usage.calls_this_session` so the model can self-budget. "
        "Pass `force_refresh=true` when the user explicitly wants fresh "
        "results or you know a previous result is stale. Per-result "
        "`raw_content` is capped at ~8k chars to protect context; the cut is "
        "visible via `raw_content_chars` / `raw_content_truncated_chars` and "
        "`truncated: true`."
    ),
)
async def web_search(
    query: str = Field(
        description=(
            "The search query. Concrete topic names work best. The query is "
            "trimmed and lowercased before any cache lookup or upstream call."
        ),
        min_length=1,
    ),
    max_results: int = Field(
        default=5,
        description="Max results to return. Clamped to [1, 10] server-side.",
    ),
    force_refresh: bool = Field(
        default=False,
        description=(
            "Bypass the on-disk cache and always re-query Tavily even when a "
            "fresh-enough entry exists. Use when the user explicitly asks "
            "for fresh results or you know a previous result is stale."
        ),
    ),
    ctx: Context | None = None,
) -> dict:
    """Web search via Tavily and return structured results."""
    state = _state_from(ctx)
    settings: Settings = state["settings"]
    cache: DiskCache = state["cache"]
    rate_limiter: MinIntervalLimiter = state["rate_limiter"]
    call_counter: CallCounter = state["call_counter"]
    tavily: TavilyClient | None = state["tavily"]

    echoed = query.strip()
    if not echoed:
        return {
            "query": "",
            "results": [],
            "request_id": "",
            "cache": "miss",
            "note": EMPTY_SEARCH_NOTE,
            "usage": call_counter.snapshot(),
        }
    n = clamp_count(max_results)

    # 1. Cache lookup — the polite path; no network, no credit burn.
    if not force_refresh:
        cached = cache.get(echoed)
        if cached is not None and cached.kind == "search":
            cached_results = cached.response.get("results", [])[:n]
            cached_note = cached.response.get("note", "")
            cached_answer = cached.response.get("answer", "")
            cached_request_id = cached.response.get("request_id", "")
            return {
                "query": echoed,
                "answer": cached_answer,
                "results": cached_results,
                "request_id": cached_request_id,
                "cache": "hit",
                "note": cached_note,
                "usage": call_counter.snapshot(),
            }

    # 2. Bail early if the Tavily client is not configured.
    if tavily is None:
        raise ToolError(
            "TAVILY_API_KEY is not set. Set it in .env and restart the server, "
            "or use a free sibling search tool (e.g. duck-search-mcp)."
        )

    # 3. Pace upstream calls.
    await rate_limiter.wait()

    # 4. Hit Tavily.
    try:
        upstream = await tavily.search(
            query=echoed,
            max_results=n,
            search_depth=settings.search_depth,
            topic=settings.topic,
            include_answer=settings.include_answer,
            include_raw_content=settings.include_raw_content,
            include_favicon=settings.include_favicon,
            chunks_per_source=settings.chunks_per_source,
        )
    except TavilyAuthError as e:
        raise ToolError(
            "Tavily rejected the API key. Check TAVILY_API_KEY is set "
            "correctly and the key has not been revoked."
        ) from e
    except TavilyQuotaError as e:
        raise ToolError(
            f"{e} Switch to a free sibling search tool (e.g. duck-search-mcp) "
            "or wait for the dashboard quota to reset."
        ) from e
    except TavilyRateLimitError as e:
        raise ToolError(
            "Tavily is rate-limiting this key. Wait a few seconds and try "
            "again, or fall back to a free sibling search tool."
        ) from e
    except TavilyError as e:
        raise ToolError(f"web_search upstream error: {e}") from e

    call_counter.charge()
    results = upstream["results"][:n]
    note = "" if results else EMPTY_SEARCH_NOTE
    response = {
        "query": echoed,
        "answer": upstream.get("answer", "") or "",
        "results": results,
        "request_id": upstream.get("request_id", "") or "",
        "cache": "bypassed" if force_refresh else "miss",
        "note": note,
        "usage": call_counter.snapshot(),
    }
    # The on-disk cache holds the upstream-mapped shape minus the live
    # counter, so a cache hit re-injects the snapshot at response time.
    # We cache the full upstream `results` list (post-truncation) so future
    # calls with the same query get the same body, regardless of which
    # `max_results` value the caller passes.
    cache.set(
        echoed,
        "search",
        {"query": echoed},
        {
            "query": echoed,
            "answer": upstream.get("answer", "") or "",
            "results": upstream["results"],
            "request_id": upstream.get("request_id", "") or "",
            "note": note,
        },
    )
    return response


@mcp.tool(
    name="extract_url",
    description=(
        "Extract clean content from one or more URLs via Tavily. Accepts 1-"
        "20 URLs in a single batch and returns per-URL `raw_content` plus a "
        "separate `failed_results` array for any URL that could not be "
        "extracted (HTTP 200 can have entries in both). Per-URL "
        "`raw_content` is capped at ~8k chars; the cut is visible via "
        "`raw_content_chars` / `raw_content_truncated_chars` and `truncated: "
        "true`. Pass `force_refresh=true` when the user explicitly wants "
        "fresh content or you know a page has changed. Every response "
        "includes `usage.calls_this_session` so the model can self-budget."
    ),
)
async def extract_url(
    urls: list[str] = Field(
        description=(
            "The URL(s) to extract. Either a single URL string or a list of "
            "1-20 URLs; both forms are accepted, batch is preferred for "
            "small sets. Each entry must be a syntactically valid http(s) "
            "URL."
        ),
        min_length=1,
    ),
    force_refresh: bool = Field(
        default=False,
        description=(
            "Bypass the on-disk cache and always re-query Tavily even when a "
            "fresh-enough entry exists."
        ),
    ),
    ctx: Context | None = None,
) -> dict:
    """Extract one or more URLs via Tavily and return per-URL bodies + failures."""
    state = _state_from(ctx)
    settings: Settings = state["settings"]
    cache: DiskCache = state["cache"]
    rate_limiter: MinIntervalLimiter = state["rate_limiter"]
    call_counter: CallCounter = state["call_counter"]
    tavily: TavilyClient | None = state["tavily"]

    # Normalise to a list and validate. Pydantic guarantees `urls` is a list
    # of strings per the schema; we still defensively coerce single strings.
    if isinstance(urls, str):
        urls_list = [urls]
    else:
        urls_list = list(urls)
    cleaned: list[str] = []
    for u in urls_list:
        u = (u or "").strip()
        if not u:
            continue
        parsed = urllib.parse.urlsplit(u)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ToolError(f"extract_url requires an http(s) URL; got {u!r}")
        cleaned.append(u)
    if not cleaned:
        raise ToolError("extract_url requires at least one URL")
    if len(cleaned) > 20:
        raise ToolError(f"extract_url accepts 1-20 URLs; got {len(cleaned)}")

    batch_key = canonical_batch_key(cleaned)

    if not force_refresh:
        cached = cache.get(batch_key)
        if cached is not None and cached.kind == "extract":
            return {
                "urls": cleaned,
                "results": cached.response.get("results", []),
                "failed_results": cached.response.get("failed_results", []),
                "request_id": cached.response.get("request_id", "") or "",
                "cache": "hit",
                "note": cached.response.get("note", ""),
                "usage": call_counter.snapshot(),
            }

    if tavily is None:
        raise ToolError(
            "TAVILY_API_KEY is not set. Set it in .env and restart the server."
        )

    await rate_limiter.wait()

    try:
        upstream = await tavily.extract(
            urls=cleaned,
            extract_depth=settings.extract_depth,
            format=settings.extract_format,
            include_favicon=settings.extract_include_favicon,
        )
    except TavilyAuthError as e:
        raise ToolError(
            "Tavily rejected the API key. Check TAVILY_API_KEY is set "
            "correctly and the key has not been revoked."
        ) from e
    except TavilyQuotaError as e:
        raise ToolError(
            f"{e} Wait for the dashboard quota to reset, or top up the plan."
        ) from e
    except TavilyRateLimitError as e:
        raise ToolError(
            "Tavily is rate-limiting this key. Wait a few seconds and try again."
        ) from e
    except TavilyError as e:
        raise ToolError(f"extract_url upstream error: {e}") from e

    call_counter.charge()
    results = upstream["results"]
    failed = upstream["failed_results"]
    # Note is set when no URL produced a usable body. The `failed_results`
    # array carries the per-URL error; the model reads it explicitly.
    note = EMPTY_EXTRACT_NOTE if not results else ""

    response = {
        "urls": cleaned,
        "results": results,
        "failed_results": failed,
        "request_id": upstream.get("request_id", "") or "",
        "cache": "bypassed" if force_refresh else "miss",
        "note": note,
        "usage": call_counter.snapshot(),
    }
    cache.set(
        batch_key,
        "extract",
        {"urls": cleaned},
        {
            "results": results,
            "failed_results": failed,
            "request_id": upstream.get("request_id", "") or "",
            "note": note,
        },
    )
    return response


# -------------------------------------------------------------------- helpers --


def _state_from(ctx: Context | None) -> dict:
    if ctx is None or ctx.request_context is None:
        raise RuntimeError("tool called without a request context")
    lifespan_state = ctx.request_context.lifespan_context
    if not lifespan_state:
        raise RuntimeError("server lifespan did not initialise state")
    return lifespan_state
