"""Environment-driven configuration for the server.

Single source of truth for defaults and env-var parsing. All settings are
read at process start; nothing here mutates global state.
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    return default if raw is None or raw == "" else raw


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise ValueError(f"{name} must be an integer; got {raw!r}") from e


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as e:
        raise ValueError(f"{name} must be a float; got {raw!r}") from e


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _resolve_log_level() -> int:
    raw = _env_str("MCP_LOG_LEVEL", _env_str("FASTMCP_LOG_LEVEL", "WARNING")).upper()
    mapping = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "WARN": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }
    if raw not in mapping:
        raise ValueError(f"MCP_LOG_LEVEL must be one of {sorted(mapping)}; got {raw!r}")
    return mapping[raw]


def _resolve_include(raw: str, *, field: str) -> bool | str:
    """Tavily `include_answer` / `include_raw_content`: bool or "basic"/"advanced"
    (answer) or "markdown"/"text" (raw_content). Anything truthy that isn't a
    recognised keyword maps to True."""
    v = raw.strip().lower()
    if v in {"false", "0", "no", "off"}:
        return False
    if v in {"true", "1", "yes", "on"}:
        return True
    if field == "include_answer" and v in {"basic", "advanced"}:
        return v
    if field == "include_raw_content" and v in {"markdown", "text"}:
        return v
    raise ValueError(
        f"{field} must be a boolean or a recognised keyword; got {raw!r}"
    )


@dataclass(frozen=True)
class Settings:
    api_key: str
    transport: str
    host: str
    port: int
    path: str
    cache_dir: Path
    cache_ttl_secs: int
    max_raw_chars: int
    min_interval_ms: int
    http_timeout_s: float
    log_level: int
    tavily_base_url: str
    search_depth: str
    include_answer: bool | str
    include_raw_content: bool | str
    include_favicon: bool
    topic: str
    chunks_per_source: int
    extract_depth: str
    extract_format: str
    extract_include_favicon: bool

    @classmethod
    def from_env(cls) -> "Settings":
        transport = _env_str("TAVILY_TRANSPORT", "http").lower()
        if transport not in {"http", "stdio"}:
            raise ValueError(f"TAVILY_TRANSPORT must be 'http' or 'stdio'; got {transport!r}")
        max_raw_chars = _env_int("TAVILY_MAX_RAW_CHARS", 8000)
        if max_raw_chars < 256:
            raise ValueError("TAVILY_MAX_RAW_CHARS must be >= 256")
        min_interval_ms = _env_int("TAVILY_MIN_INTERVAL_MS", 200)
        if min_interval_ms < 0:
            raise ValueError("TAVILY_MIN_INTERVAL_MS must be >= 0")
        http_timeout_s = _env_float("TAVILY_HTTP_TIMEOUT_S", 60.0)
        if http_timeout_s <= 0:
            raise ValueError("TAVILY_HTTP_TIMEOUT_S must be > 0")
        cache_ttl_secs = _env_int("TAVILY_CACHE_TTL_SECS", 86400)
        if cache_ttl_secs < 0:
            raise ValueError("TAVILY_CACHE_TTL_SECS must be >= 0")
        port = _env_int("TAVILY_PORT", 8000)
        if not (1 <= port <= 65535):
            raise ValueError(f"TAVILY_PORT must be in [1, 65535]; got {port}")

        search_depth = _env_str("TAVILY_SEARCH_DEPTH", "advanced").lower()
        if search_depth not in {"basic", "advanced", "fast", "ultra-fast"}:
            raise ValueError(
                f"TAVILY_SEARCH_DEPTH must be one of {{basic, advanced, fast, ultra-fast}}; "
                f"got {search_depth!r}"
            )
        extract_depth = _env_str("TAVILY_EXTRACT_DEPTH", "basic").lower()
        if extract_depth not in {"basic", "advanced"}:
            raise ValueError(f"TAVILY_EXTRACT_DEPTH must be 'basic' or 'advanced'; got {extract_depth!r}")
        extract_format = _env_str("TAVILY_EXTRACT_FORMAT", "markdown").lower()
        if extract_format not in {"markdown", "text"}:
            raise ValueError(f"TAVILY_EXTRACT_FORMAT must be 'markdown' or 'text'; got {extract_format!r}")
        topic = _env_str("TAVILY_TOPIC", "general").lower()
        if topic not in {"general", "news", "finance"}:
            raise ValueError(f"TAVILY_TOPIC must be general|news|finance; got {topic!r}")
        chunks_per_source = _env_int("TAVILY_CHUNKS_PER_SOURCE", 3)
        if not (1 <= chunks_per_source <= 3):
            raise ValueError(f"TAVILY_CHUNKS_PER_SOURCE must be in [1, 3]; got {chunks_per_source}")

        return cls(
            api_key=(os.environ.get("TAVILY_API_KEY") or "").strip(),
            transport=transport,
            host=_env_str("TAVILY_HOST", "127.0.0.1"),
            port=port,
            path=_env_str("TAVILY_PATH", "/mcp"),
            cache_dir=Path(_env_str("TAVILY_CACHE_DIR", "./cache/tavily")).expanduser().resolve(),
            cache_ttl_secs=cache_ttl_secs,
            max_raw_chars=max_raw_chars,
            min_interval_ms=min_interval_ms,
            http_timeout_s=http_timeout_s,
            log_level=_resolve_log_level(),
            tavily_base_url=_env_str("TAVILY_BASE_URL", "https://api.tavily.com").rstrip("/"),
            search_depth=search_depth,
            include_answer=_resolve_include(
                _env_str("TAVILY_INCLUDE_ANSWER", "basic"), field="include_answer"
            ),
            include_raw_content=_resolve_include(
                _env_str("TAVILY_INCLUDE_RAW_CONTENT", "markdown"),
                field="include_raw_content",
            ),
            include_favicon=_env_bool("TAVILY_INCLUDE_FAVICON", True),
            topic=topic,
            chunks_per_source=chunks_per_source,
            extract_depth=extract_depth,
            extract_format=extract_format,
            extract_include_favicon=_env_bool("TAVILY_EXTRACT_INCLUDE_FAVICON", True),
        )

    @property
    def api_key_set(self) -> bool:
        return bool(self.api_key)


def configure_logging(level: int) -> None:
    """Logs go to stderr; stdout is the JSON-RPC stream."""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
        force=True,
    )


def clamp_count(value: int | None) -> int:
    """Clamp `max_results` to [1, 10]. None or out-of-range values are clamped."""
    if value is None:
        return 5
    return max(1, min(10, int(value)))
