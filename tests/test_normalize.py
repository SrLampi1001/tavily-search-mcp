"""Pure-function tests for query / URL normalisation, slug, hash, and batch keys."""
from __future__ import annotations

from tavily_search_mcp.normalize import (
    cache_path_for,
    fnv1a64,
    canonical_batch_key,
    normalize_query,
    normalize_url,
    slugify,
)


def test_normalize_trims_and_lowercases():
    assert normalize_query("  Rust   Programming  Language  ") == "rust programming language"


def test_normalize_collapses_inner_whitespace():
    assert normalize_query("a\t\t b\n\nc") == "a b c"


def test_normalize_strips_edges():
    assert normalize_query("   hello   ") == "hello"


def test_normalize_url_lowercases_scheme_and_host():
    assert normalize_url("HTTPS://Example.COM/path?q=1") == "https://example.com/path?q=1"


def test_normalize_url_strips_fragment():
    # Fragments are client-side and shouldn't be part of the cache key.
    assert normalize_url("https://example.com/page#section") == "https://example.com/page"


def test_normalize_non_url_passes_through_after_strip():
    # Non-URL inputs (queries) are just trimmed.
    assert normalize_url("not a url") == "not a url"


def test_slugify_handles_specials():
    assert slugify("c++") == "c"
    assert slugify("c#") == "c"
    # The hash disambiguates them.
    assert fnv1a64("c++") != fnv1a64("c#")


def test_slugify_empty_falls_back():
    assert slugify("") == "empty"
    assert slugify("---") == "empty"


def test_slugify_truncates():
    long = "a" * 200
    assert len(slugify(long)) == 80


def test_fnv1a64_deterministic():
    a = fnv1a64("hello world")
    b = fnv1a64("hello world")
    assert a == b
    assert isinstance(a, int)
    assert 0 <= a < 2**64


def test_fnv1a64_changes_with_input():
    assert fnv1a64("a") != fnv1a64("b")


def test_cache_path_for_query(tmp_path):
    p = cache_path_for("  Rust Language  ", tmp_path)
    assert p.parent == tmp_path
    assert p.name.startswith("rust-language-")
    assert p.name.endswith(".json")
    assert len(p.stem.split("-")[-1]) == 16  # 16 hex chars = 64-bit hash


def test_cache_path_for_url(tmp_path):
    p = cache_path_for("https://Example.com/path?q=1", tmp_path)
    assert p.parent == tmp_path
    assert p.name.startswith("https-example-com-")
    assert p.name.endswith(".json")


def test_cache_path_for_batch_key(tmp_path):
    # A batch JSON renders the JSON itself as the slug source.
    p = cache_path_for('["https://b.com", "https://a.com"]', tmp_path)
    assert p.parent == tmp_path
    assert p.name.endswith(".json")
    # Has a 16-char hex hash.
    assert len(p.stem.split("-")[-1]) == 16


def test_canonical_batch_key_sorts_urls():
    a = canonical_batch_key(["https://b.com/p", "https://a.com/q"])
    b = canonical_batch_key(["https://a.com/q", "https://b.com/p"])
    assert a == b


def test_canonical_batch_key_single_url_collapses():
    a = canonical_batch_key(["https://example.com/x"])
    b = canonical_batch_key(["HTTPS://Example.com/x", "https://example.com/x#frag"])
    # Both normalise to the same URL.
    assert a == b


def test_canonical_batch_key_handles_invalid():
    # Non-URL strings still normalise to themselves; the key is the JSON
    # of the sorted list. Test the dedup pass — duplicates collapse.
    a = canonical_batch_key(["https://a.com", "https://a.com"])
    b = canonical_batch_key(["https://a.com"])
    assert a == b
