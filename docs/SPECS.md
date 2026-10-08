# `rust-agent-tavily-mcp` — MCP server

> **Status:** spec only, not yet implemented. The behaviour described  below is the **minimum required to be considered a viable general-purpose  web-search tool** for the agent. Implementation happens in this repo;  integration with the agent happens by editing `config/mcp.toml` in the  `rust-agent` repo.

A [Model Context Protocol](https://modelcontextprotocol.io/) server that exposes a single agent-tuned web-search tool to the `rust-agent` over stdio:

| Tool | Backend | Cost | Best for |
| --- | --- | --- | --- |
| `web_search` | Tavily Search API | 1,000 requests/month free tier | General, question-shaped web search with **structured results** (title, URL, content snippet, optional direct answer). Agent-tuned: result content is pre-cleaned, optionally includes an LLM-generated answer. |

The Rust agent spawns this server as a child process via the official [`rmcp`](https://github.com/modelcontextprotocol/rust-sdk) crate, lists the tool, and registers it as a regular `rig::tool::Tool`. From the agent's point of view the tool is indistinguishable from any other.

---

## 1. Why this server exists

This server is one of **three** that together replace the current bundled `mcp-servers/web-search/` MCP. The bundle is being split so each backend (DDG, Firecrawl, Tavily) can be enabled, disabled, scaled, and rate-limited independently. The agent code is **unaware** of the split — it still sees one `web_search`-shaped tool per server, with the server-name prefix disambiguating which backend answered.

Tavily is the **general-purpose** path. Where the DDG tool is limited to entity-like queries, and the Firecrawl tool spends credits to return full page bodies, Tavily is designed for AI-agent consumption from day one: it returns clean, structured results across the full web (no Instant-Answer-API limitations) with a content snippet per result that's already LLM-friendly. The free tier (1,000 requests/month) is generous for an agent that budgets its queries.

The other two MCPs in the set:

- **DuckDuckGo** for entity-like queries (free, no key, snippets only).
- **Firecrawl** for deep content extraction (paid; returns full page bodies as markdown).

Together the three give the agent a layered search strategy:

| Need | Reach for |
| --- | --- |
| Entity lookup (person, place, product, standard) | `duckduckgo__web_search` |
| General question-shaped query with clean snippets | `tavily__web_search` ← **this tool** |
| Long-tail / question-shaped query that needs the **full page body** as markdown | `firecrawl__web_search` |

The tool description (§3) and the agent's preamble (see [`src/agent/prompt.rs`](https://github.com/example/rust-agent/blob/main/src/agent/prompt.rs)) encode this layering.

---

## 2. Protocol contract

The agent (per [`docs/mcp.md`](https://github.com/example/rust-agent/blob/main/docs/mcp.md) §8) requires:

1. Speaks **MCP 2024-11-05** (the version `rmcp 0.8` negotiates).
2. Accepts JSON-args objects on `tools/call`.
3. Returns either `structured_content` (preferred) or at least one text
   content block.
4. Advertises at least one tool on `tools/list`.

Anything beyond this contract is a server-side decision.

---

## 3. Tool

### `web_search` (single tool)

**Input schema** (JSON Schema, surfaced verbatim to the model):

```json
{
  "type": "object",
  "properties": {
    "query":     { "type": "string",  "description": "..." },
    "max_results": { "type": "integer", "minimum": 1, "maximum": 10,
                     "default": 5,      "description": "..." },
    "include_answer": { "type": "boolean", "default": true,
                        "description": "..." },
    "search_depth":   { "type": "string",  "enum": ["basic", "advanced"],
                        "default": "basic", "description": "..." }
  },
  "required": ["query"],
  "additionalProperties": false
}
```

| Argument | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `query` | string | yes | – | The search query. |
| `max_results` | integer | no | 5 | Number of results to return. Clamped to `[1, 10]`. |
| `include_answer` | bool | no | `true` | Whether to ask Tavily for a direct LLM-generated answer in addition to the result list. The answer (when present) is a single short paragraph; the model is told to verify it against the result URLs. |
| `search_depth` | string | no | `"basic"` | `"basic"` (faster, fewer tokens, default) or `"advanced"` (slower, returns more content per result, costs more on paid plans). The `advanced` mode also crawls the result pages — for a full-page-body experience the right tool is `firecrawl__web_search`. |

**Output** (`structured_content`, a JSON object):

```json
{
  "query": "what is the recommended way to configure a custom JSON logger in tokio",
  "answer": "Use the tracing crate's json formatter with a subscriber...",
  "results": [
    { "title":   "...",
      "url":     "...",
      "content": "...",
      "score":   0.92,
      "raw_content": null }
  ],
  "source": "upstream" | "cache",
  "request_id": "..."
}
```

| Field | Type | Notes |
| --- | --- | --- |
| `query` | string | The query as the server received it. |
| `answer` | string or `null` | A direct LLM-generated answer from Tavily, when `include_answer: true`. The model is told (in the tool description) to verify this against the result URLs — the answer can be wrong. |
| `results` | array of `{title, url, content, score, raw_content}` | Mapped from Tavily's response. Capped at `max_results`. |
| `source` | `"upstream"` or `"cache"` | Whether the response was served from the on-disk cache. |
| `request_id` | string | Tavily's per-request ID. Echoed for operator-side debugging and quota reconciliation. |

**Per-result fields:**

| Field | Type | Notes |
| --- | --- | --- |
| `title` | string | Page title from the search result. |
| `url` | string | Canonical URL. |
| `content` | string | LLM-friendly content snippet from the result page. Already cleaned (no HTML, no boilerplate). Capped per Tavily's defaults (~1k chars). |
| `score` | float | Tavily's relevance score in `[0, 1]`. Not part of the tool description — operators may use it for analytics; the model should not over-index on it. |
| `raw_content` | string or `null` | The un-cleaned raw content from the page. Populated by Tavily only for `search_depth: "advanced"`. For full-page-body needs, prefer `firecrawl__web_search`. |

**Description (the model reads this verbatim):**

> Free-tier, agent-tuned web search via Tavily. Reach for this when the query is **question-shaped or long-tail** and the `duckduckgo__web_search` tool's empty-response note is firing too often. Tavily returns clean, structured snippets (no HTML, no boilerplate) for any web query — entity-like, question-shaped, or comparison. Costs nothing on the free tier up to 1,000 calls/month. When `include_answer` is on (default) the response also includes a direct LLM-generated answer; treat that as a hint and verify it against the `results` URLs before quoting. For full page bodies as markdown, use `firecrawl__web_search` instead. Use `max_results` to cap the result list (default 5, max 10).

---

## 4. Tavily API surface (v1)

The server uses the official `tavily-python` SDK. The relevant v1 endpoints:

| Endpoint | Use |
| --- | --- |
| `POST /search` | The main tool. Body: `{api_key, query, max_results, include_answer, search_depth, ...}`. |
| `GET /usage` | Optional. Per-month usage snapshot (used only if the server is asked to surface it via a future `credits`-like field; not required for v0.1). |

**Authentication:** the `api_key` is passed in the request body (not in a header), per Tavily's convention. The server reads `TAVILY_API_KEY` from the environment and threads it through.

**Rate limits:** the free tier is 1,000 requests/month. The `search_depth: "advanced"` mode also has per-request rate limits on some plans. The server should **not** implement its own retry policy beyond the SDK default; rate-limit errors are surfaced as `is_error: true` and the model decides whether to fall back.

---

## 5. Configuration

All configuration is via environment variables. The Rust agent forwards the relevant ones when it spawns the server via the `env_pass` allow-list in `config/mcp.toml` — see the agent's [`docs/mcp.md`](https://github.com/example/rust-agent/blob/main/docs/mcp.md) §3 for the env-forwarding semantics.

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `TAVILY_API_KEY` | yes | – | Tavily API key. Get a free one at <https://tavily.com>. Free tier: 1,000 requests/month. |
| `TAVILY_CACHE_DIR` | no | `./cache/tavily` | On-disk cache directory. Gitignored. |
| `TAVILY_CACHE_TTL_SECS` | no | `86400` | Cache freshness window, seconds. `0` disables. |
| `TAVILY_MIN_INTERVAL_MS` | no | `200` | Minimum spacing between upstream Tavily calls. Tavily's free tier is generous but the polite default is to pace, not burst. |
| `MAX_RESULTS_PER_CALL` | no | `10` | Hard upper bound the `max_results` argument is clamped to. Mirrors the DDG and Firecrawl servers. |
| `DEFAULT_RESULTS_PER_CALL` | no | `5` | Default value when `max_results` is omitted. Mirrors the DDG and Firecrawl servers. |
| `MCP_LOG_LEVEL` | no | `WARNING` | `DEBUG` / `INFO` / `WARNING`. Logs go to **stderr** (stdout is the JSON-RPC stream). |

**API key is required.** The server refuses to start (lazy — on first call) without it.

---

## 6. Caching

On-disk TTL cache, key = normalised query (trimmed, inner whitespace collapsed, lowercased). File name = `<slug>-<fnv1a64>.json`. Envelope:

```json
{ "fetched_at_unix": 1730000000,
  "query": "...",
  "answer": "...",
  "results": [ ... ],
  "request_id": "..." }
```

**Failure semantics:** a missing, stale, corrupt, or unwritable cache entry **never fails the call**. The tool logs a `warn` and degrades to a plain uncached call.

**Caching does not consume Tavily quota.** That's the whole point of the cache. The `source: "cache"` field in the response makes this visible to the model (useful for the model's own budgeting).

The cache directory is gitignored. Delete any time to force fresh lookups.

---

## 7. Install / setup

```bash
cd rust-agent-tavily-mcp
python3 -m venv .venv      # or: uv venv --python 3.12 .venv
source .venv/bin/activate
pip install -e .
```

**Dependencies (suggested):**

- `fastmcp>=2.0` — MCP server framework. (The project is `prefecthq/fastmcp` on GitHub but published as `fastmcp` on PyPI; it is the standard framework for building MCP servers in Python and powers most of the Python MCP ecosystem.)
- `tavily-python>=0.5` — official Tavily SDK. Exposes the v1 `search()` and the optional `get_usage()`.

Tested with Python 3.10+. Other interpreters are fine as long as the `rmcp` 0.8 client on the agent side negotiates `2024-11-05` (the version `fastmcp` 2.x ships).

---

## 8. Run by hand (for debugging)

```bash
# Set the API key first.
export TAVILY_API_KEY=tvly-...

# The server speaks JSON-RPC over stdio. A blank stdin will let
# it sit idle; pipe a real `initialize` + `tools/list` exchange
# to see the registered tool schema.
python3 -m tavily_mcp
```

---

## 9. Integration with `rust-agent`

Add a single entry to `config/mcp.toml` in the `rust-agent` repo:

```toml
[[mcp.servers]]
name = "tavily"
command = ["/abs/path/to/rust-agent-tavily-mcp/.venv/bin/python",
           "/abs/path/to/rust-agent-tavily-mcp/tavily_mcp/__main__.py"]
env_pass = ["TAVILY_API_KEY", "PATH", "HOME"]
enabled = true
```

Restart the agent. The model now sees the tool under its qualified name `tavily__web_search`. To temporarily disable without removing the entry, set `enabled = false` (the agent parses and validates the entry but skips spawning).

The agent's `.env` (or wherever the operator keeps the key) should export `TAVILY_API_KEY`. The agent's `config/mcp.toml` entry's `env_pass` allow-list forwards it to the spawned subprocess.

The default `web-search` MCP entry in `config/mcp.toml` (the bundled DDG + Firecrawl one) **must be removed or disabled** before the new Tavily entry is enabled, or the agent will register two `web_search` shaped tools and the model will be confused about which to use.

---

## 10. Failure modes and how the model reacts

| What goes wrong | What the tool does | What the model sees |
| --- | --- | --- |
| `TAVILY_API_KEY` not set | `is_error: true` on first call (lazy — no startup probe without a key) | "Tavily API key not set; reach for `duckduckgo__web_search` instead, or set `TAVILY_API_KEY` in the agent's environment." |
| `TAVILY_API_KEY` invalid (`401`) | `is_error: true` with the 401 message | "Tavily rejected the API key." Operator notices; model falls back. |
| Tavily rate limit (`429`) | The SDK retries with backoff (default 3 attempts); on exhaustion, `is_error: true` | A rate-limit error. The model can wait or fall back. |
| Tavily monthly quota exhausted | `is_error: true` with a clear message | "Tavily quota exhausted for this billing period. Use `duckduckgo__web_search`." Model falls back. |
| Cache directory unwritable | Logs `warn`; falls through to a plain uncached call | The tool still works, just slower on repeat queries (and quota will be spent). |
| Cache file corrupt | Logs `warn`; falls through to a fresh upstream call | Same. |
| Tavily endpoint changes shape | The mapper may return `results: []` for previously-working queries | Empty results without a `note` (Tavily is not entity-API-shaped — empty means "nothing matched"). The model can rephrase. |
| MCP server child dies | The `rmcp` client surfaces an `ErrorData`; the tool returns a protocol-level error | A connection error. The supervisor loop's "two empty iterations" rule nudges the model. |
| Server didn't spawn (venv missing or no key) | `build_agent` returns an error at agent startup | The agent refuses to start with a clear message naming the server and the command. **No silent fallback** — the design choice documented in `docs/mcp.md` §7. |

---

## 11. Context-budget protection

Tavily's per-result `content` is already capped server-side (Tavily returns ~1k chars per result by default, regardless of `max_results`). A 5-result call therefore adds at most ~5k tokens — well under the per-iteration budget. No per-result markdown cap is required on the client side; if the operator wants to be more aggressive, a `TAVILY_MAX_CONTENT_CHARS` cap can be added in a follow-up.

The agent's existing `RUST_AGENT_CONTEXT_PRESSURE_THRESHOLD_TOKENS` guard (default 800k, sized for MiniMax-M3's 1M-token window) covers the multi-call case end-to-end.

---

## 12. Acceptance criteria (minimum viable)

The implementation is "good enough to unblock the agent" when **all** of the following hold:

1. `pip install -e .` from a clean checkout succeeds on Python 3.10+.
2. `python3 -m tavily_mcp` starts and responds to a manual `initialize` + `tools/list` JSON-RPC exchange.
3. The advertised tool is exactly `web_search`, with the input schema and description in §3.
4. A live call against Tavily with a real key returns at least one result with title, URL, and content snippet.
5. When `include_answer: true` (the default), the response includes a non-empty `answer` field.
6. Two identical calls within the cache TTL produce a second response with `source: "cache"` and no upstream traffic.
7. Setting `TAVILY_API_KEY` to an invalid value (or unsetting it on a real key) returns `is_error: true` with a clear message.
8. The `max_results` argument is clamped to `[1, 10]`; values outside that range are clamped, not rejected.
9. Spawning this server from the agent's `config/mcp.toml` succeeds, the model sees the tool as `tavily__web_search`, and a one-shot request (`cargo run -- run --request "What is the recommended way to configure a custom JSON logger in tokio?"`) triggers at least one call to it and ends with a non-empty final reply that cites at least one URL from the results.

---

## 13. Test plan

Unit tests (hermetic, no network):

- `cache_round_trip_serves_second_call_from_disk` — write a cache entry, read it back, assert `source: "cache"`.
- `cache_corrupt_file_falls_through_to_upstream` — write garbage to a cache file, assert the call still succeeds (with a `warn` log).
- `count_is_clamped_to_1_through_10` — assert `max_results=0` clamps to `1`, `max_results=11` clamps to `10`, `max_results=None` (omitted) defaults to `5`.
- `query_is_trimmed_and_echoed` — assert `"  hello  "` is stored as `"hello"` and returned in the `query` field.
- `missing_api_key_returns_clear_error` — unset `TAVILY_API_KEY`, assert the first call returns `is_error: true` with a "key not set" message.
- `search_depth_is_passed_through` — assert the `search_depth` argument is forwarded to the Tavily SDK unchanged.

Integration tests (network, gated on the venv and a real key existing):

- `mcp_server_advertises_web_search` — handshake, `tools/list`, assert exactly one tool named `web_search`.
- `basic_search_returns_results_with_answer` — call with `include_answer: true`, assert `results.len() >= 1` and `answer` is non-empty.
- `basic_search_long_tail_returns_results` — call a known-long-tail query that DDG would have returned empty for, assert at least one result (this is the marquee test for this server).
- `second_call_within_ttl_serves_from_cache` — call twice, assert the second has `source: "cache"`.
- `invalid_api_key_returns_clear_error` — point the server at an invalid key, assert the call returns `is_error: true` with a 401-style message.

End-to-end (manual, smoke checklist from `docs/testing.md`):

- `cargo run -- run --request "Design a tokio JSON logger skill set"`
  — assert the trace contains at least one `tavily__web_search` call, and the final Markdown cites at least one URL from the results.

---

## 14. Out of scope for v0.1

- **No Tavily Extract / Crawl / Research endpoints.** The single tool is `/search`. Extract (single-URL structured extraction), Crawl (whole-site crawl), and Research (multi-step deep research) are all out of scope for v0.1; if the agent ever needs them, they can be added as additional tools on the same server.
- **No usage / quota introspection tool.** A `credits`-like field is not in the response shape (Tavily doesn't surface remaining quota via the search endpoint; the optional `get_usage()` call is per-month, not per-remaining). The agent is told to budget by counting its own calls. If a quota tool is added later, it would be a second tool on this server, not a new MCP.
- **No rate-limit-aware retries beyond the SDK default.** The Tavily SDK retries 429s with backoff. Custom retry policies are out of scope.
- **No HTTP transport.** Stdio only, per `docs/mcp.md` §6. When the agent's HTTP transport lands, this server may be wrapped behind a small stdio-to-HTTP shim, or a parallel HTTP entry point can be added later — out of scope for v0.1.
- **No `include_raw_content` / `raw_content` parsing.** When `search_depth: "advanced"`, Tavily can return `raw_content` per result. The response echoes it through (the field is in the per-result shape) but the server does not parse, sanitise, or cache it specially. For full-page-body needs, use `firecrawl__web_search`.

---

## 15. References

- Agent integration overview: [`docs/mcp.md`](https://github.com/example/rust-agent/blob/main/docs/mcp.md) in the `rust-agent` repo.
- Agent's `tools::mcp` module-level reference: [`docs/modules/tools-mcp.md`](https://github.com/example/rust-agent/blob/main/docs/modules/tools-mcp.md).
- Existing bundled DDG + Firecrawl MCP (the thing being split): [`mcp-servers/web-search/`](https://github.com/example/rust-agent/tree/main/mcp-servers/web-search) in the `rust-agent` repo. The current `mcp.toml` example for a `tavily` entry in the bundled MCP's docs is the direct ancestor of the integration snippet in §9.
- Tavily API docs: <https://docs.tavily.com>.
- `tavily-python` SDK: <https://github.com/tavily-ai/tavily-python>.
- MCP specification: <https://modelcontextprotocol.io>.
- `fastmcp` (Python MCP server framework): <https://github.com/PrefectHQ/fastmcp>.
