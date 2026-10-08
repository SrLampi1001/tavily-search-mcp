# `tavily-search-mcp` — MCP server

> **Status:** v0.1 implemented. Speaks MCP 2026-07-28 over Streamable HTTP and stdio, built on FastMCP 4.0.11. The behaviour described below is the contract this server commits to.

A [Model Context Protocol](https://modelcontextprotocol.io/specification/2026-07-28) server that exposes the Tavily `/search` and `/extract` endpoints as MCP tools for any modern AI agent. No agent-specific integration; runs as a standalone streamable HTTP server that any modern MCP client can connect to.

| Tool | Backend | Best for |
| --- | --- | --- |
| `web_search` | Tavily `/search` | Question-shaped, long-tail, or generally web-shaped queries. Returns clean, LLM-friendly snippets with optional direct answer and per-result `raw_content`. Charges Tavily credits. |
| `extract_url` | Tavily `/extract` | Fetching clean content for 1–20 known URLs in a single call. Charges per-URL credits. |

| Prompt | Use |
| --- | --- |
| `research_topic` | Plan a single `web_search` call, including the empty-result fallback and the call-counter budget reminder. |
| `fetch_document` | Plan a single `extract_url` call with a focus question; reminds the model about the per-URL `raw_content` cap and `failed_results`. |

---

## 1. Why this server exists

Tavily is the general-purpose path against a credit budget. Where the [DuckDuckGo Instant Answer API](https://duckduckgo.com/api) only answers entity-shaped queries and returns empty `200`s for long-tail / question-shaped input, Tavily is designed for AI-agent consumption from day one: it returns clean, structured results across the full web (no Instant-Answer-API limitations) with a content snippet per result that's already LLM-friendly. The signup tier comes with 1,000 credits/month; pass-as-you-go applies after consumption — the server deliberately treats credit spend as budget-by-self-count, since Tavily has no live remaining-balance endpoint.

This server does not try to replace deep page-body extraction. For deep-content needs that warrant the spend, a sibling Firecrawl MCP is the right tool. `extract_url` here is the lightweight path for a small, known set of URLs the model already has pointers to.

### 1.1 Why a self-hosted MCP and not Tavily's hosted one

Tavily also exposes an [official hosted MCP](https://docs.tavily.com/documentation/mcp.md) at `mcp.tavily.com/mcp/` — pick that when you want Tavily with zero setup. Pick this server when you want knobs the hosted service doesn't expose: an env-driven search profile (depth, answer mode, raw_content, favicon, topic, chunks-per-source), on-disk TTL caching that absorbs repeat queries without burning credits, a per-call `usage.calls_this_session` budgeting signal from an in-process counter, `research_topic` / `fetch_document` prompts tuned for the empty-result fallback, and a loopback-only bind with no extra relay.

---

## 2. Protocol contract

The server is built on **FastMCP 4.0.11** and targets the **MCP 2026-07-28** revision. It is dual-era out of the box — modern clients negotiate the stateless protocol, legacy clients that send `initialize` still get a working session.

| Contract | Value |
| --- | --- |
| Protocol revision | `2026-07-28` (advertised via `server/discover`; legacy `initialize` negotiates `2025-11-25`) |
| Transports | Streamable HTTP at `/mcp` (default), stdio (opt-in via `TAVILY_TRANSPORT=stdio`) |
| Bind address | `127.0.0.1` (loopback only; expose via VS Code port forwarding or ngrok — see `docs/DEPLOYMENT.md`) |
| Authentication | **None.** Loopback binding is the only access control. Operate behind a trusted tunnel. |
| Required response fields | `MCP-Protocol-Version`, `Mcp-Method`, `Mcp-Name` headers; tool list advertises `ttlMs`/`cacheScope` |
| Server features | `tools` (two), `prompts` (two), `resources/templates` (none), `extensions: io.modelcontextprotocol/ui` |

Any modern MCP client (Claude Desktop, Cursor, opencode, VS Code Copilot Chat) that supports Streamable HTTP can connect to the forwarded URL without code changes.

---

## 3. Tools

### 3.1 `web_search`

**Input schema** (JSON Schema, surfaced verbatim to the model):

```json
{
  "type": "object",
  "properties": {
    "query":         { "type": "string",  "description": "...",
                       "minLength": 1 },
    "max_results":   { "type": "integer", "description": "...",
                       "default": 5 },
    "force_refresh": { "type": "boolean", "description": "...",
                       "default": false }
  },
  "required": ["query"],
  "additionalProperties": false
}
```

| Argument | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `query` | string | yes | – | The search query. Trimmed before any cache lookup or upstream call; the trimmed query is echoed in the response. |
| `max_results` | integer | no | 5 | Max results to return. **Clamped** to `[1, 10]` server-side (out-of-range values are not rejected, they are clamped). |
| `force_refresh` | boolean | no | false | Bypass the on-disk cache and re-query Tavily even when a fresh-enough entry exists. |

**Output** (`structured_content`, a JSON object):

```json
{
  "query": "how to configure a custom JSON logger in tokio",
  "answer": "Use the tracing crate's json formatter...",
  "results": [
    {
      "title": "...",
      "url":   "...",
      "content": "...",
      "score": 0.92,
      "raw_content": "...",
      "raw_content_chars": 1234,
      "raw_content_truncated_chars": 1234,
      "truncated": false,
      "published_date": null,
      "favicon": "...",
      "id": "..."
    }
  ],
  "request_id": "abc-123",
  "cache": "miss" | "hit" | "bypassed",
  "note": "",
  "usage": { "calls_this_session": 3 }
}
```

| Field | Type | Notes |
| --- | --- | --- |
| `query` | string | The query as the server received it (after `trim()`). |
| `answer` | string | A direct LLM-generated answer from Tavily when `TAVILY_INCLUDE_ANSWER` is truthy (default `"basic"`). May be an empty string if Tavily declined to answer. Treat it as a hint, verify against `results` URLs. |
| `results` | array | Mapped from Tavily's `results[]`. Capped at `max_results`. |
| `request_id` | string | Tavily's per-request ID. Echoed for operator-side debugging and quota reconciliation. |
| `cache` | `"miss"` / `"hit"` / `"bypassed"` | Tells the model whether this call hit the cache, the network, or whether the caller opted out of the cache. |
| `note` | string | Non-empty when the result list is empty; the model should rephrase or fall back to its own knowledge. |
| `usage` | object | `calls_this_session` is the running counter for upstream Tavily calls in the current process. Tavily does not expose a live credit-balance endpoint; the model uses this counter to self-budget. |

**Per-result fields:**

| Field | Type | Notes |
| --- | --- | --- |
| `title` | string | Page title from the Tavily result. |
| `url` | string | Canonical URL. |
| `content` | string | LLM-friendly snippet (~1k chars by default from Tavily). Already cleaned (no HTML, no boilerplate). |
| `score` | float | `[0, 1]` relevance score. Surface it but don't put it in the tool description. |
| `raw_content` | string | The cleaned raw page body when `TAVILY_INCLUDE_RAW_CONTENT` is truthy (default `"markdown"`). Per-result length is capped at `TAVILY_MAX_RAW_CHARS` (default 8000). |
| `raw_content_chars` | integer | Original body length before any truncation. |
| `raw_content_truncated_chars` | integer | The cut length. Equal to `raw_content_chars` when no truncation happened. |
| `truncated` | boolean | `true` when `raw_content` was truncated. |
| `published_date` | string or null | Tavily's best estimate of publication date; only with `include_published_date=true` or `topic=news`. |
| `favicon` | string or null | URL of the favicon when `TAVILY_INCLUDE_FAVICON=true` (default). |
| `id` | string | Stable per-result ID; surface it so callers can dedupe across calls. |

**Description (the model reads this verbatim):**

> Web search via Tavily. Reach for this when the question is question-shaped, long-tail, or generally web-shaped and you want clean, LLM-ready snippets (not just links). Tavily charges credits per call (the signup tier is 1,000 credits/month; `advanced` depth is 2 credits, `basic`/`fast`/`ultra-fast` are 1); every response includes `usage.calls_this_session` so the model can self-budget. Pass `force_refresh=true` when the user explicitly wants fresh results or you know a previous result is stale. Per-result `raw_content` is capped at ~8k chars to protect context; the cut is visible via `raw_content_chars` / `raw_content_truncated_chars` and `truncated: true`.

### 3.2 `extract_url`

**Input schema:**

```json
{
  "type": "object",
  "properties": {
    "urls": {
      "type": "array",
      "items": { "type": "string" },
      "minItems": 1,
      "maxItems": 20,
      "description": "..."
    },
    "force_refresh": { "type": "boolean", "default": false, "description": "..." }
  },
  "required": ["urls"],
  "additionalProperties": false
}
```

| Argument | Type | Required | Default | Notes |
| --- | --- | --- | --- | --- |
| `urls` | list of strings | yes | – | 1–20 URLs. Single URL → `urls: ["https://..."]`. Must be syntactically valid `http(s)` URLs. The cache key is the canonical-JSON encoding of the sorted, normalised URLs. |
| `force_refresh` | boolean | no | false | Bypass the on-disk cache. |

**Output** (`structured_content`, a JSON object):

```json
{
  "urls": ["https://example.com/a", "https://example.com/b"],
  "results": [
    {
      "url": "https://example.com/a",
      "raw_content": "...",
      "raw_content_chars": 4500,
      "raw_content_truncated_chars": 4500,
      "truncated": false,
      "images": [],
      "favicon": "..."
    }
  ],
  "failed_results": [
    { "url": "https://example.com/b", "error": "Page behind a paywall" }
  ],
  "request_id": "abc-123",
  "cache": "miss" | "hit" | "bypassed",
  "note": "",
  "usage": { "calls_this_session": 4 }
}
```

| Field | Type | Notes |
| --- | --- | --- |
| `urls` | list of strings | The URLs the server received, normalised (whitespace-trimmed). |
| `results` | array | Per-URL successful extractions. Each entry: `{url, raw_content, raw_content_chars, raw_content_truncated_chars, truncated, images, favicon}`. |
| `failed_results` | array | Per-URL failures. Each entry: `{url, error}`. **HTTP 200 can have entries here** — Tavily returns per-URL failures even when the request itself succeeded. Surface every entry. |
| `request_id` | string | Tavily's per-request ID. |
| `cache` | `"miss"` / `"hit"` / `"bypassed"` | Tells the model whether this call hit the cache, the network, or whether the caller opted out. |
| `note` | string | Non-empty when `results` is empty (every URL ended up in `failed_results`). The `failed_results` array carries the per-URL error; the model reads it explicitly. |
| `usage` | object | `calls_this_session`. One extract call increments the counter by 1 (Tavily charges per URL inside the batch, not per call). |

**Description (the model reads this verbatim):**

> Extract clean content from one or more URLs via Tavily. Accepts 1–20 URLs in a single batch and returns per-URL `raw_content` plus a separate `failed_results` array for any URL that could not be extracted (HTTP 200 can have entries in both). Per-URL `raw_content` is capped at ~8k chars; the cut is visible via `raw_content_chars` / `raw_content_truncated_chars` and `truncated: true`. Pass `force_refresh=true` when the user explicitly wants fresh content or you know a page has changed. Every response includes `usage.calls_this_session` so the model can self-budget.

---

## 4. Response mapping (Tavily → MCP)

Both tools apply a thin mapping layer from Tavily's raw JSON onto the flat `structured_content` documented above.

### 4.1 `web_search`

Tavily returns:

```json
{
  "query": "...",
  "answer": "...",
  "results": [
    {
      "title": "...",
      "url": "...",
      "content": "...",
      "score": 0.9,
      "raw_content": "...",
      "favicon": "...",
      "id": "..."
    }
  ],
  "response_time": 0.42,
  "request_id": "..."
}
```

The mapper drops entries without a resolved `url`, caps the list at `max_results` (post-clamp), and applies the per-result `raw_content` truncation (when `raw_content` is non-null and longer than `TAVILY_MAX_RAW_CHARS`).

### 4.2 `extract_url`

Tavily returns:

```json
{
  "results": [
    {
      "url": "...",
      "raw_content": "...",
      "images": [],
      "favicon": "..."
    }
  ],
  "failed_results": [
    { "url": "...", "error": "..." }
  ],
  "response_time": 0.0,
  "request_id": "..."
}
```

Both arrays come through as-is; the mapper applies the per-URL `raw_content` truncation and drops empty-URL entries. HTTP 200 can return an empty `results[]` and a populated `failed_results[]` — that is a successful call, not an error.

---

## 5. Configuration

All configuration is via environment variables. See `.env.example` for the full list with defaults.

| Variable | Default | Purpose |
| --- | --- | --- |
| `TAVILY_API_KEY` | – | Tavily API key. **Required for both tools.** |
| `TAVILY_TRANSPORT` | `http` | `http` (Streamable HTTP) or `stdio`. |
| `TAVILY_HOST` | `127.0.0.1` | Bind address for HTTP transport. Loopback by design. |
| `TAVILY_PORT` | `8000` | Port for HTTP transport. |
| `TAVILY_PATH` | `/mcp` | URL path for the MCP endpoint. |
| `TAVILY_CACHE_DIR` | `./cache/tavily` | On-disk cache directory. Gitignored. |
| `TAVILY_CACHE_TTL_SECS` | `86400` | Cache freshness window in seconds. `0` disables the cache. |
| `TAVILY_MAX_RAW_CHARS` | `8000` | Per-result cap on `raw_content` (~2k tokens). |
| `TAVILY_MIN_INTERVAL_MS` | `200` | Minimum spacing between upstream Tavily calls in ms. `0` disables. |
| `TAVILY_HTTP_TIMEOUT_S` | `60` | Upstream HTTP timeout in seconds. |
| `TAVILY_SEARCH_DEPTH` | `advanced` | `basic` / `advanced` / `fast` / `ultra-fast`. |
| `TAVILY_INCLUDE_ANSWER` | `basic` | Boolean or `basic` / `advanced`. The signup tier pays the same for the LLM answer regardless of mode. |
| `TAVILY_INCLUDE_RAW_CONTENT` | `markdown` | Boolean or `markdown` / `text`. |
| `TAVILY_INCLUDE_FAVICON` | `true` | Whether to include per-result favicons. |
| `TAVILY_TOPIC` | `general` | `general` / `news` / `finance`. |
| `TAVILY_CHUNKS_PER_SOURCE` | `3` | Joined chunks per result (1–3). |
| `TAVILY_EXTRACT_DEPTH` | `basic` | `basic` / `advanced` (advanced = 2 credits per 5 successful URLs vs 1). |
| `TAVILY_EXTRACT_FORMAT` | `markdown` | `markdown` / `text`. |
| `TAVILY_EXTRACT_INCLUDE_FAVICON` | `true` | Per-URL favicon for `extract_url`. |
| `MCP_LOG_LEVEL` | `WARNING` | `DEBUG` / `INFO` / `WARNING` / `ERROR`. Logs go to **stderr** (stdout is the JSON-RPC stream). `FASTMCP_LOG_LEVEL` is honoured as a fallback. |

---

## 6. Caching

On-disk TTL cache covering both tools. Key = normalised input. For `web_search`, the key is the trimmed, inner-whitespace-collapsed, lowercased query. For `extract_url`, the key is the JSON encoding of the sorted list of normalised URLs, so a single-URL call and a one-URL batch collide. File name = `<slug>-<fnv1a64>.json`.

Envelope:

```json
{
  "fetched_at_unix": 1730000000,
  "kind": "search" | "extract",
  "input": { "query": "..." } | { "urls": [...] },
  "response": { ... mapped response minus the live usage block ... }
}
```

The cache stores the **full, untruncated** body. Truncation to `TAVILY_MAX_RAW_CHARS` is a presentation-time concern that the tools apply at response time, not at storage time. The `usage` block is **not** cached — every call returns a fresh counter view, but a cache hit does NOT increment the counter.

**Failure semantics:** a missing, stale, corrupt, or unwritable cache entry **never fails the call**. The tool logs a `warn` and degrades to a plain uncached call.

**Bypass:** both tools take a `force_refresh: bool` arg; when `true`, the cache is bypassed and the response's `cache` field reports `"bypassed"` so the model can verify.

The cache directory is gitignored. Delete any time to force fresh lookups.

---

## 7. Call counting and budget guidance

Tavily has no live remaining-credit endpoint. The signup tier is 1,000 credits/month; the server cannot peek at the balance. Two choices were on the table, and the architecture chosen is **budget by self-counting**:

1. **In-memory counter** — every successful upstream call increments `CallCounter.calls_this_session`. The same value is surfaced on every response as `usage.calls_this_session`. Two processes sharing one API key each count their own calls.
2. **Polite pacing** — `TAVILY_MIN_INTERVAL_MS` (default 200 ms) spaces upstream calls so a runaway agent loop can't burst the budget out of a single process.

The model is told in the tool description and in the `research_topic` / `fetch_document` prompts to switch to a free sibling search tool (e.g. `duck-search-mcp`) once the counter grows large. Operators who want a hard ceiling can lower `TAVILY_CACHE_TTL_SECS` and increase `TAVILY_MIN_INTERVAL_MS` to slow drift.

There is no third "credit_status" tool: Tavily's optional `GET /usage` is a per-month summary, not a live balance, and adding a tool that returns just that would only encourage the model to chase a number that doesn't exist.

---

## 8. Privacy & Tavily Terms

Tavily's [Terms of Service](https://www.tavily.com/terms) §6.5 and §9.2 explicitly state that Tavily and its third-party AI providers may use, retain, and process Customer Input and Outputs for purposes of training, improving, and developing their AI models. **Any query or URL you send through this server may be retained by Tavily for model-improvement purposes.** Operators with strict data-handling requirements should not use the Tavily paid tier through this MCP, or should scope the API key to a plan that includes a training-opt-out.

The on-disk cache is operator-local — under `TAVILY_CACHE_DIR`, gitignored, deleted when the directory is removed. Tavily never sees what the cache stores.

---

## 9. Prompts

### 9.1 `research_topic`

A reusable user-message prompt that sets up a single `web_search` call. Args: `topic: str`. Returns a user message that:

1. Tells the model to use `web_search` with a canonical topic name (not a question).
2. Sets `max_results=5`.
3. Defines the empty-result fallback: rephrase once, then fall back to the model's own knowledge and say so.
4. Includes the call-counter reminder: every response includes `usage.calls_this_session`; the model is told to switch to a free sibling search tool once the count grows large.
5. Includes the truncation reminder: when `truncated: true`, fall back to `extract_url` on the same URL with a focus.

Registered on the same FastMCP instance as the tools. No extra setup.

### 9.2 `fetch_document`

A reusable user-message prompt that sets up a single `extract_url` call with `urls` and `focus`. Returns a user message that:

1. Tells the model to use `extract_url` with the URL(s) as a JSON array (up to 20 per call).
2. Asks the model to summarise the bodies around `focus`, dropping irrelevant sections before quoting.
3. Reminds the model that per-URL `raw_content` is capped at ~8k chars and the cut is visible via `truncated: true`.
4. Reminds the model that `failed_results` is the right place to look when URLs don't return content.

Registered on the same FastMCP instance as the tools. No extra setup.

---

## 10. Install / setup

```bash
cd tavily-search-mcp
python3 -m venv .venv           # or: uv venv --python 3.12 .venv
source .venv/bin/activate
pip install -e .                # or: uv pip install -e .
cp .env.example .env
$EDITOR .env
```

**Dependencies (pinned, per the project's `pyproject.toml`):**

- `fastmcp==4.0.11` — MCP server framework, the standalone PrefectHQ package.
- `httpx>=0.27` — async HTTP client. The Tavily v1 API is a JSON-over-HTTPS surface with a Bearer token; an SDK adds no value here, and httpx gives us fail-open caching and timeout control with a single dependency.

Tested with Python 3.12. Works on 3.10+.

---

## 11. Run by hand

```bash
# HTTP (default) — listen on http://127.0.0.1:8000/mcp
python3 -m tavily_search_mcp

# Stdio — for clients that spawn the server as a child process
TAVILY_TRANSPORT=stdio python3 -m tavily_search_mcp
```

A blank stdin in stdio mode lets the server sit idle. Point any MCP client (opencode, Claude Desktop, VS Code, Cursor) at the URL or spawn the process; see `docs/DEPLOYMENT.md` for the connection recipes.

---

## 12. Deployment

Two supported paths. Both rely on the server binding to `127.0.0.1`; neither requires the server itself to know it's being forwarded.

1. **VS Code "Forward a Port"** (recommended). Open the Ports panel, forward `8000`, copy the URL, point your client at `<forwarded-url>/mcp`. The tunnel runs through Microsoft's relay — no public hostname to block.
2. **ngrok** (fallback). `ngrok http 8000`, point your client at `<ngrok-url>/mcp`. Free-tier URLs are publicly enumerable; corporate networks often block them.

For both, the server is started with `python3 -m tavily_search_mcp`. No auth is configured — the loopback bind + tunnel auth is the access control. See `docs/DEPLOYMENT.md` for the full step-by-step.

---

## 13. Failure modes and how the model reacts

| What goes wrong | What the tool does | What the model sees |
| --- | --- | --- |
| `TAVILY_API_KEY` not set | Both tools raise `ToolError("TAVILY_API_KEY is not set...")` on the first call | A clear error. Operator notices; no accidental credit spend. |
| `TAVILY_API_KEY` invalid (401) | `ToolError("Tavily rejected the API key...")` | A clear HTTP error. Operator notices; model falls back. |
| Tavily quota exhausted (432 / 433) | `ToolError("...quota exhausted...Switch to a free sibling...")` | A budget error. The model can switch to `duck-search-mcp` or wait. |
| Tavily rate limit (429) | `ToolError("Tavily is rate-limiting this key...")` | A rate-limit error. The model can wait or fall back. |
| Per-URL extraction failure | `extract_url` returns the URL in `failed_results` with `error` populated; HTTP 200 is still 200 | A normal success where one URL is explicitly flagged. The model reads `failed_results` and reacts. |
| `raw_content` is `null` per result | The mapper renders `raw_content: ""`; `raw_content_chars: 0` | A normal result with empty raw_content; the model uses `content` (the snippet) instead. |
| `result_count` is `[]` | `web_search` returns `results: []` with a non-empty `note` | A guidance note. The model can rephrase or fall back to its own knowledge. |
| A result's page body exceeds the cap | Per-URL truncation to `TAVILY_MAX_RAW_CHARS`, with a marker and `truncated: true` | A normal success with a clearly marked cut. The model can call `extract_url` on the same URL with a focus. |
| Cache directory unwritable | Logs `warn`; falls through to a plain uncached call | The tool still works, just slower on repeat queries (and credits will be spent). |
| Cache file corrupt | Logs `warn`; falls through to a fresh upstream call | Same. |
| Foreign `Origin` header | Accepted when the server is bound to `127.0.0.1` (DNS rebinding isn't possible) | The probe flags this as a WARN; see `docs/DEPLOYMENT.md` for when to enable `http_host_origin_protection`. |
| Server didn't start (port in use, venv missing, key missing) | Process exits with a clear log line on stderr | The client gets a connection error. No silent fallback. |

---

## 14. Acceptance criteria

The implementation is "good enough to ship" when **all** of the following hold:

1. `pip install -e .` from a clean checkout succeeds on Python 3.10+. ✅
2. With a real `TAVILY_API_KEY` set, `python3 -m tavily_search_mcp` starts on `http://127.0.0.1:8000/mcp` and answers `server/discover` with `supportedVersions: ["2026-07-28"]`. ✅
3. The advertised tools are exactly `web_search` and `extract_url`, with the input schemas and descriptions in §3. ✅
4. The advertised prompts are exactly `research_topic` and `fetch_document`. ✅
5. A live `web_search` call against Tavily with a real key returns at least one result with title, URL, and content. ✅
6. The response includes `usage.calls_this_session` and a non-zero counter after the first upstream call. ✅
7. A second identical `web_search` call within the cache TTL produces a response with `cache: "hit"` and **no** increment to the call counter. ✅
8. A single-URL `extract_url` call returns the page body and per-URL metadata. ✅
9. A batch `extract_url` call with one URL in `failed_results` includes the failure with an `error` field. ✅
10. A second identical `extract_url` call within the cache TTL produces `cache: "hit"`. ✅
11. Setting `TAVILY_API_KEY` to an invalid value (or unsetting it on a real key) returns `ToolError` with a clear message from the affected tools. ✅
12. A result whose page body exceeds `TAVILY_MAX_RAW_CHARS` is truncated and the `raw_content_chars` / `raw_content_truncated_chars` fields are populated correctly. ✅
13. The wire probe (`.agents/skills/mcp-fastmcp-2026/scripts/probe_mcp_server.py`) reports 0 failures against the live server. ✅
14. The in-process test suite (`pytest`) reports 0 failures in both `mode="auto"` and `mode="legacy"`. ✅

---

## 15. Test plan

Unit tests (hermetic, no network) live in `tests/`:

- `test_normalize.py` — query and URL normalisation, slug, FNV-1a 64-bit hash, canonical batch key.
- `test_cache.py` — round trip, normalisation equivalence, stale entry, corrupt file, unwritable dir, atomic write, kinds.
- `test_rate_limit.py` — disabled at 0 ms, serial waits spaced, concurrent waits queued.
- `test_call_counter.py` — increment, thread safety, snapshot isolation.
- `test_tavily.py` — search and extract response mapping (entry filtering, raw_content truncation, null raw_content), error mapping (401/429/432/433/500), Bearer header propagation through a recording transport.
- `test_server.py` — tool/prompt advertisement, basic search + extract, count clamp, empty result, cache hit, both client modes, force_refresh bypass, all error paths, batch extract with `failed_results`.
- `test_server_integration.py` — real Tavily calls, skipped unless `TAVILY_NETWORK_TESTS=1`.

End-to-end (manual):

- `python3 -m tavily_search_mcp` then connect from any modern MCP client (opencode, VS Code, Claude Desktop, Cursor) and confirm `web_search` and `extract_url` appear and return results.
- Confirm `usage.calls_this_session` increments only on upstream calls (cache hits do not bump it).

---

## 16. Out of scope for v0.1

- **No other Tavily endpoints.** No `/crawl` (whole-site crawl), no `/map` (URL discovery), no `/research` (multi-step deep research), no `/feedback`, no `/usage`, no `/logs`. The two tools cover search and single-batch extraction only. If a future need arises, it can be added as a third tool on the same server.
- **No keyless access.** Although Tavily offers keyless `/search` and `/extract` via `X-Tavily-Access-Mode: keyless`, this server is configured for an authenticated key. Adding keyless mode is straightforward but out of scope for v0.1.
- **No advanced search knobs in the tool schema.** `topic`, `include_answer`, `include_raw_content`, `chunks_per_source`, `country`, `language`, `exclude_domains`, `exact_match`, etc. are all server-side env-configurable (`TAVILY_TOPIC`, `TAVILY_INCLUDE_ANSWER`, ...) and not exposed per-call. The start-up prompt's "Minimum tool surface" choice.
- **No async polling.** None of the async Tavily endpoints (none in the current `/search` / `/extract` shape, but future ones may be) are exposed.
- **No rate-limit-aware retries beyond a single attempt.** The shared `min_interval_ms` mutex spaces calls, and `429` / `432` / `433` responses surface to the model. Custom retry policies with backoff are out of scope.
- **No proxy or per-call Tavily options override.** The defaults come from the env-driven profile. Operators can change them server-side.
- **No parallel upstream calls.** A single in-flight upstream request is the unit. The shared `min_interval_ms` mutex serialises them.

---

## 17. References

- MCP specification (target revision): https://modelcontextprotocol.io/specification/2026-07-28/
- MCP `server/discover`: https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/discover
- Tavily v1 API: https://docs.tavily.com/
- Tavily `/search` reference: https://docs.tavily.com/documentation/api-reference/endpoint/search
- Tavily `/extract` reference: https://docs.tavily.com/documentation/api-reference/endpoint/extract
- Tavily Terms of Service: https://www.tavily.com/terms
- Tavily credits & pricing: https://docs.tavily.com/documentation/api-credits
- FastMCP (Python MCP server framework): https://github.com/PrefectHQ/fastmcp and https://gofastmcp.com
- VS Code Streamable HTTP support: https://code.visualstudio.com/api/extension-guides/ai/mcp
