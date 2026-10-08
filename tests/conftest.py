"""Shared pytest fixtures."""
from __future__ import annotations

import os

import pytest


@pytest.fixture
def tmp_cache_dir(monkeypatch) -> str:
    """Per-test cache dir, env-pointed at it, cleaned up after."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory(prefix="tavily-cache-") as d:
        path = Path(d)
        monkeypatch.setenv("TAVILY_CACHE_DIR", str(path))
        yield str(path)


@pytest.fixture
def isolated_env(monkeypatch):
    """Reset the env to a known clean state for tests that read settings."""
    for key in list(os.environ):
        if key.startswith(
            ("TAVILY_", "MCP_LOG_LEVEL", "FASTMCP_LOG_LEVEL")
        ):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test-key-for-unit-tests")
    yield


@pytest.fixture(autouse=True)
def _per_test_cache(monkeypatch, tmp_path):
    """Every test gets a fresh cache directory.

    Without this, integration tests would share `./cache/tavily/` with
    previous pytest runs and could flake on a stale cache hit.
    """
    monkeypatch.setenv("TAVILY_CACHE_DIR", str(tmp_path / "tavily"))
    monkeypatch.setenv("MCP_LOG_LEVEL", "ERROR")
    monkeypatch.setenv("FASTMCP_LOG_LEVEL", "ERROR")
