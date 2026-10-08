"""Tests for the on-disk TTL cache. Per SPECS §6, cache failures must never break the call."""
from __future__ import annotations

import json
import time
from pathlib import Path

from tavily_search_mcp.cache import CachedResponse, DiskCache


def _make_cache(tmp_path: Path, ttl: int = 86400) -> DiskCache:
    return DiskCache(tmp_path, ttl)


def test_disabled_cache_returns_none(tmp_path):
    cache = _make_cache(tmp_path, ttl=0)
    assert cache.enabled is False
    assert cache.get("anything") is None
    cache.set("anything", "search", {"query": "anything"}, {"results": [], "note": ""})
    assert cache.get("anything") is None
    assert list(tmp_path.iterdir()) == []  # nothing written


def test_round_trip_serves_from_disk(tmp_path):
    cache = _make_cache(tmp_path)
    payload = {
        "results": [{"title": "Example", "url": "https://example.com", "content": "Body"}],
        "note": "",
    }
    cache.set("How do I write async code in Rust", "search",
              {"query": "How do I write async code in Rust"}, payload)

    got = cache.get("How do I write async code in Rust")
    assert got is not None
    assert got.kind == "search"
    assert got.input == {"query": "How do I write async code in Rust"}
    assert got.response == payload
    assert any(p.name.startswith("how-do-i-write-async-code-in-rust") for p in tmp_path.iterdir())


def test_normalised_query_hits_same_entry(tmp_path):
    cache = _make_cache(tmp_path)
    cache.set("  Rust   Programming  Language  ", "search",
              {"query": "  Rust   Programming  Language  "},
              {"results": [{"title": "x", "url": "u", "content": "s"}], "note": ""})
    assert cache.get("rust programming language") is not None
    assert cache.get("RUST PROGRAMMING LANGUAGE") is not None


def test_stale_entry_returns_none(tmp_path):
    cache = DiskCache(tmp_path, ttl_seconds=1)
    cache.set("q", "search", {"query": "q"}, {"results": [], "note": ""})
    time.sleep(1.2)
    assert cache.get("q") is None


def test_corrupt_file_falls_through_without_raising(tmp_path, caplog):
    cache = _make_cache(tmp_path)
    cache.set("q", "search", {"query": "q"}, {"results": [], "note": ""})
    paths = list(tmp_path.iterdir())
    assert len(paths) == 1
    paths[0].write_text("{not valid json")
    with caplog.at_level("WARNING"):
        assert cache.get("q") is None
    assert any("cache: read failed" in r.message for r in caplog.records)


def test_unknown_kind_file_falls_through(tmp_path, caplog):
    cache = _make_cache(tmp_path)
    cache.set("q", "search", {"query": "q"}, {"results": [], "note": ""})
    paths = list(tmp_path.iterdir())
    paths[0].write_text(json.dumps({
        "fetched_at_unix": time.time(),
        "kind": "unknown",
        "input": {},
        "response": {},
    }))
    with caplog.at_level("WARNING"):
        assert cache.get("q") is None
    assert any("unknown kind" in r.message for r in caplog.records)


def test_writes_are_atomic_via_tmp_rename(tmp_path):
    cache = _make_cache(tmp_path)
    cache.set("q", "search", {"query": "q"}, {"results": [], "note": ""})
    tmps = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert tmps == []


def test_unwritable_dir_does_not_raise(tmp_path, monkeypatch, caplog):
    blocker = tmp_path / "blocker"
    blocker.write_text("I am a file, not a directory.")
    blocked = blocker / "deep" / "deeper"
    cache = DiskCache(blocked, ttl_seconds=60)
    with caplog.at_level("WARNING"):
        cache.set("q", "search", {"query": "q"}, {"results": [], "note": ""})
    assert any("cache: write failed" in r.message for r in caplog.records)


def test_unreadable_file_does_not_raise(tmp_path, caplog):
    cache = _make_cache(tmp_path)
    cache.set("q", "search", {"query": "q"}, {"results": [], "note": ""})
    for p in tmp_path.iterdir():
        p.write_text("not json")
    with caplog.at_level("WARNING"):
        assert cache.get("q") is None
    assert any("cache: read failed" in r.message for r in caplog.records)


def test_invalidate_removes_entry(tmp_path):
    cache = _make_cache(tmp_path)
    cache.set("q", "search", {"query": "q"}, {"results": [], "note": ""})
    assert cache.get("q") is not None
    cache.invalidate("q")
    assert cache.get("q") is None


def test_extract_round_trip(tmp_path):
    cache = _make_cache(tmp_path)
    payload = {"results": [{"url": "https://example.com/docs", "raw_content": "Body"}],
               "failed_results": []}
    cache.set('["https://example.com/docs"]', "extract",
              {"urls": ["https://example.com/docs"]}, payload)
    got = cache.get('["https://example.com/docs"]')
    assert got is not None
    assert got.kind == "extract"
    assert got.response == payload


def test_search_and_extract_dont_collide_on_same_string(tmp_path):
    """A search key and an extract key that happen to share a normalised
    prefix should not overwrite each other; both round-trip independently.
    """
    cache = _make_cache(tmp_path)
    cache.set("example", "search", {"query": "example"}, {"results": [{"title": "t"}], "note": ""})
    cache.set('["example"]', "extract", {"urls": ["example"]}, {"results": [], "failed_results": []})
    assert cache.get("example") is not None
    assert cache.get('["example"]') is not None
