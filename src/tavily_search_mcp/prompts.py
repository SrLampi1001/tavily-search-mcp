"""MCP prompts registered on the FastMCP instance.

Per SPECS §9:
- `research_topic` — plans one `web_search` call and sets the empty-result
  fallback + the call-counter reminder.
- `fetch_document` — plans one `extract_url` call with a focus question and
  tells the model to respect the per-page `raw_content` cap.
"""
from __future__ import annotations

from fastmcp import FastMCP


def register_prompts(mcp: FastMCP) -> None:
    @mcp.prompt(
        name="research_topic",
        description=(
            "Plan a web_search call for a topic. Steers the model toward a "
            "canonical topic name, the empty-result fallback, and the call-"
            "counter reminder."
        ),
    )
    def research_topic(topic: str) -> str:
        """Generate a user message that sets up one `web_search` call."""
        return (
            f"Research the topic '{topic}'.\n"
            f"1. Use the web_search tool with a canonical topic name "
            f"(e.g. 'Rust programming language', NOT 'how do I write async code in Rust'). "
            f"Pass max_results=5.\n"
            f"2. Every response includes a `usage.calls_this_session` count. "
            f"Tavily does not expose a remaining-credit endpoint, so budget by "
            f"that counter — switch to a free sibling search tool (e.g. "
            f"`duck-search-mcp`) once the count grows large.\n"
            f"3. If the result list is empty, rephrase once as a canonical "
            f"topic name and try again.\n"
            f"4. If still empty, fall back to your own knowledge and clearly "
            f"state that the search returned no results.\n"
            f"5. When a result's `raw_content` is truncated (look for "
            f"`truncated: true` and `raw_content_truncated_chars` < `raw_content_chars`), "
            f"call `extract_url` on the same URL with a focus to see the full body."
        )

    @mcp.prompt(
        name="fetch_document",
        description=(
            "Plan an extract_url call for one or more URLs with a focus "
            "question. Tells the model to summarise around the focus and to "
            "respect the per-page raw_content cap."
        ),
    )
    def fetch_document(urls: list[str], focus: str) -> str:
        """Generate a user message that sets up one `extract_url` call."""
        url_list = ", ".join(f"'{u}'" for u in urls)
        return (
            f"Fetch the page(s) at {url_list} and answer: {focus}.\n"
            f"1. Use the extract_url tool with `urls` set to that list. Up to "
            f"20 URLs may share one call; pass them as a JSON array. The "
            f"response contains one entry per successful URL plus a separate "
            f"`failed_results` array for any URL that could not be extracted.\n"
            f"2. Per-URL `raw_content` is capped at ~8k chars; the cut is "
            f"visible via `truncated: true` and `raw_content_truncated_chars` < `raw_content_chars`.\n"
            f"3. If the body is empty or the URL appears only in "
            f"`failed_results`, summarise whatever metadata (`favicon`, "
            f"`images`) the response carries and clearly state the gap.\n"
            f"4. Every response includes `usage.calls_this_session`; budget "
            f"by that counter — extract charges are based on URLs, not on "
            f"calls, so larger batches cost proportionally more.\n"
            f"5. Quote sparingly — paraphrase around the focus, cite only "
            f"URLs the tool actually returned."
        )
