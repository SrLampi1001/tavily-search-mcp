# Tavily Search MCP

A [Model Context Protocol](https://modelcontextprotocol.io/) server that exposes the Tavily `/search` and `/extract` endpoints as MCP tools for any modern AI agent. Built on **FastMCP 4.0.11** and targeting the **MCP 2026-07-28** revision. Runs over Streamable HTTP (default) or stdio.

Tavily is the **general-purpose** path against a credit budget. `web_search` returns LLM-friendly snippets across the full web — useful for question-shaped, long-tail, or generally web-shaped queries where DuckDuckGo's Instant Answer API would return empty. `extract_url` covers the "I have a small batch of URLs and just need the pages" case in one Tavily call (1–20 URLs per request). Every response carries a `usage.calls_this_session` snapshot so the calling agent can self-budget, since Tavily has no live remaining-credit endpoint.

No auth on the server itself; loopback bind by design. Expose via **VS Code port forwarding** (recommended) or ngrok.

## What you get

- `web_search(query, max_results=5, force_refresh=False)` — Tavily `/search` with optional `include_answer`, per-result `raw_content`, favicons, and stable IDs. Returns `{title, url, content, score, raw_content, raw_content_chars, raw_content_truncated_chars, truncated, ...}` per result plus `usage.calls_this_session`.
- `extract_url(urls, force_refresh=False)` — Tavily `/extract` for 1–20 URLs in a single call. Returns per-URL `{url, raw_content, raw_content_chars, raw_content_truncated_chars, truncated, images, favicon}` plus a separate `failed_results` array for any URL that could not be extracted (HTTP 200 can have entries in both).
- `research_topic(topic)` — reusable prompt that plans one `web_search` call and defines the empty-result fallback.
- `fetch_document(urls, focus)` — reusable prompt that plans one `extract_url` call and tells the model to summarise around the focus and respect the per-URL cap.
- On-disk TTL cache (24 h default) so repeat queries within the window don't burn credits.
- Configurable minimum-interval pacing so a runaway agent loop can't burst the budget out of a single process.

## Why a self-hosted MCP and not Tavily's hosted MCP?

Tavily also exposes an [official hosted MCP](https://docs.tavily.com/documentation/mcp.md) at `mcp.tavily.com/mcp/`. It's the right choice when you want Tavily with zero setup. This server is the right choice when you want knobs Tavily's hosted service doesn't expose:

- **Cache** — the hosted MCP has none; ours has an on-disk TTL cache that absorbs repeat queries without burning credits.
- **Profile config** — `TAVILY_SEARCH_DEPTH`, `TAVILY_INCLUDE_ANSWER`, `TAVILY_MAX_RAW_CHARS`, etc. are env-driven and operator-controlled.
- **Budgeting signal** — every response carries `usage.calls_this_session` from an in-process counter, so a model that loops can see its own spend.
- **Local-only** — runs in your loopback; no extra relay you have to trust.
- **Prompts** — `research_topic` and `fetch_document` encode the empty-result fallback and per-URL cap in user-facing text the model reads.

## Quick start

```bash
# Install (Python 3.10+)
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# Configure the Tavily API key (signup: 1,000 credits/month at https://app.tavily.com)
cp .env.example .env
$EDITOR .env

# Run (Streamable HTTP on http://127.0.0.1:8000/mcp)
python3 -m tavily_search_mcp
```

Then either:

- **Local use** — point an MCP client at `http://127.0.0.1:8000/mcp` on the same machine.
- **VS Code port forwarding** — open the Ports panel, forward `8000`, copy the tunnel URL, point your client at `<forwarded-url>/mcp`. See [docs/DEPLOYMENT.md](./docs/DEPLOYMENT.md).
- **ngrok** — `ngrok http 8000`, use the printed URL the same way.

## Documentation

- [docs/SPECS.md](./docs/SPECS.md) — the contract this server commits to: protocol, tool schemas, response mapping, failure modes, acceptance criteria.
- [docs/DEPLOYMENT.md](./docs/DEPLOYMENT.md) — connection recipes for every supported client, plus hardening notes for non-loopback exposure.
- [.agents/skills/mcp-fastmcp-2026/](./.agents/skills/mcp-fastmcp-2026/) — the skill this server was built against, kept in-tree for future agents and for the static audit and wire-probe scripts.

## Testing

```bash
.venv/bin/pytest                                  # unit tests, hermetic
TAVILY_NETWORK_TESTS=1 .venv/bin/pytest            # + integration tests, real Tavily
.venv/bin/python .agents/skills/mcp-fastmcp-2026/scripts/audit_mcp_project.py .   # static
```

After starting the server:

```bash
.venv/bin/python .agents/skills/mcp-fastmcp-2026/scripts/probe_mcp_server.py http://127.0.0.1:8000/mcp   # wire probe
```

## Why the limits are where they are

Tavily's signup tier is **1,000 credits/month**. `search_depth=advanced` costs 2 credits per call (1 for `basic`/`fast`/`ultra-fast`); `extract_url` costs 1 credit per 5 successful URLs at `extract_depth=basic` (2 at `advanced`). The defaults (`max_results=5`, `TAVILY_CACHE_TTL_SECS=86400`, `TAVILY_MAX_RAW_CHARS=8000`, `TAVILY_MIN_INTERVAL_MS=200`) keep a single session from draining the budget in a handful of loops while still letting the model answer substantive queries. The per-result `raw_content` cap protects the calling agent's context window.

Every `web_search` and `extract_url` response includes `usage.calls_this_session` — the running counter of upstream Tavily calls in the current process. The model uses it to self-budget; it switches to a free sibling search tool (e.g. `duck-search-mcp`) once the count grows large.

## Notes

- **Privacy.** Per [Tavily's Terms of Service](https://www.tavily.com/terms) §6.5 / §9.2, Tavily and its third-party AI providers may retain and use any Customer Input and Outputs to train models. Treat queries as potentially visible to Tavily. The on-disk cache is operator-local (gitignored, under `TAVILY_CACHE_DIR`); Tavily never sees what the cache stores. See [SPECS §8](./docs/SPECS.md#8-privacy--tavily-terms) for the full breakdown.
- **No credit endpoint.** Tavily has no live remaining-balance endpoint, so this server has no `credit_status` tool. The architecture choice is to count calls in-process and surface them; SPECS §7 explains why this is the right trade.
- **Auth.** The Authorization header is the wire contract (`Authorization: Bearer tvly-...`). The token never appears in the request body. The SDK is dropped on purpose (httpx alone is enough).
