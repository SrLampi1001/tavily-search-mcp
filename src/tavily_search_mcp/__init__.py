"""tavily-search-mcp — Tavily-backed MCP server.

Exposes Tavily /search and /extract as MCP tools, with on-disk TTL caching,
a min-interval rate limiter, and an in-process call counter (Tavily has no
live credit-balance endpoint). See docs/SPECS.md for the contract this
package commits to.
"""
from __future__ import annotations

__version__ = "0.1.0"
